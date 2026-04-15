package com.knowledgeeng.bridge.extract;

import com.github.javaparser.*;
import com.github.javaparser.ast.*;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.*;
import com.github.javaparser.ast.type.*;
import com.github.javaparser.resolution.declarations.ResolvedMethodDeclaration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.*;
import com.knowledgeeng.bridge.config.ModuleConfig;
import com.knowledgeeng.bridge.model.*;
import com.knowledgeeng.bridge.progress.ProgressReporter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Collectors;

/**
 * 核心文件处理器：扫描 Java 文件，提取实体和关系。
 * 用 JavaParser + SymbolSolver 替代 javalang + 正则。
 */
public class JavaFileProcessor {

    private final Path repoRoot;
    private final ModuleConfig config;
    private final ProgressReporter reporter;
    private final StructureFacts facts = new StructureFacts();

    // 线程安全缓存
    private final Set<String> createdPackages = ConcurrentHashMap.newKeySet();
    private final Map<String, String> classIds = new ConcurrentHashMap<>();     // (relPath|className) → classId
    private final Map<String, String> methodIds = new ConcurrentHashMap<>();    // (classId|sig) → methodId
    private final Map<String, String> packageByPath = new ConcurrentHashMap<>(); // relPath → packageName
    private final List<DeferredCall> deferredCalls = Collections.synchronizedList(new ArrayList<>());
    private final List<String> parseErrors = Collections.synchronizedList(new ArrayList<>());

    // 单实例 JavaParser（AST解析阶段是顺序的，IO阶段并行）
    private JavaParser sharedParser;

    // 并发度
    private final int maxWorkers;

    private static final int MAX_SNIPPET = 12000;

    public JavaFileProcessor(Path repoRoot, ModuleConfig config, ProgressReporter reporter) {
        this(repoRoot, config, reporter, Runtime.getRuntime().availableProcessors());
    }

    public JavaFileProcessor(Path repoRoot, ModuleConfig config, ProgressReporter reporter, int maxWorkers) {
        this.repoRoot = repoRoot;
        this.config = config;
        this.reporter = reporter;
        this.maxWorkers = Math.max(1, maxWorkers);
        this.sharedParser = createParser();
    }

    /** 每个线程创建独立的 JavaParser 实例（SymbolSolver 不线程安全） */
    private JavaParser createParser() {
        CombinedTypeSolver typeSolver = new CombinedTypeSolver();
        typeSolver.add(new ReflectionTypeSolver(false));

        for (ModuleConfig.Module mod : config.getModules()) {
            Path srcRoot = repoRoot;
            if (mod.getPath() != null && !mod.getPath().isEmpty()) {
                srcRoot = repoRoot.resolve(mod.getPath());
            }
            for (String srcDir : List.of("src/main/java", "src/main/generated", "src")) {
                Path candidate = srcRoot.resolve(srcDir);
                if (Files.isDirectory(candidate)) {
                    try {
                        typeSolver.add(new JavaParserTypeSolver(candidate));
                    } catch (Exception ignored) {}
                }
            }
        }

        JavaSymbolSolver symbolSolver = new JavaSymbolSolver(typeSolver);
        ParserConfiguration parserConfig = new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21)
                .setSymbolResolver(symbolSolver);

        return new JavaParser(parserConfig);
    }

    /**
     * 处理所有文件，返回 StructureFacts。
     */
    public StructureFacts extractAll() {
        List<String> files = new ArrayList<>(config.getFileModuleMap().keySet());
        Collections.sort(files);
        int total = files.size();
        AtomicInteger processed = new AtomicInteger(0);

        // Phase 1: 并行读取文件内容（IO 密集，多线程有效）
        reporter.progress(0, total, "Reading " + total + " files...");
        Map<String, String> fileContents = new ConcurrentHashMap<>();
        {
            ExecutorService ioPool = Executors.newFixedThreadPool(Math.min(maxWorkers, 8));
            List<Future<?>> ioFutures = new ArrayList<>();
            for (String relPath : files) {
                ioFutures.add(ioPool.submit(() -> {
                    try {
                        Path fullPath = repoRoot.resolve(relPath);
                        if (Files.exists(fullPath)) {
                            fileContents.put(relPath, Files.readString(fullPath, StandardCharsets.UTF_8));
                        }
                    } catch (Exception ignored) {}
                }));
            }
            ioPool.shutdown();
            try { ioPool.awaitTermination(2, TimeUnit.MINUTES); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }

        // Phase 2: 顺序 AST 解析（SymbolSolver 非线程安全，但内存中已有文件内容，IO 无阻塞）
        for (int i = 0; i < total; i++) {
            String relPath = files.get(i);
            String moduleId = config.getModuleId(relPath);
            if (i % 100 == 0 || i == total - 1) {
                reporter.progress(i + 1, total, "Parsing " + relPath);
            }
            String content = fileContents.get(relPath);
            if (content == null) continue;

            try {
                processFileFromContent(relPath, moduleId, content);
            } catch (Exception e) {
                parseErrors.add(relPath + ": " + e.getMessage());
                reporter.fileError(relPath, e.getClass().getSimpleName() + ": " + e.getMessage());
            }
        }

        // 全局阶段：解析延迟方法调用
        resolveDeferredCalls();

        // 跨服务边提取
        if (config.isExtractCrossService()) {
            extractCrossServiceEdges();
            extractFeignBindings();
        }

        // 关系属性填充
        enrichRelationAttributes();

        // 元数据
        facts.setMeta("repo_path", repoRoot.toString());
        facts.setMeta("language", "java");
        facts.setMeta("entity_id_scheme", "canonical_v1");
        facts.setMeta("parser", "javaparser-bridge-1.0.0");
        facts.setMeta("parse_errors", parseErrors);

        reporter.done(facts.getEntities().size(), facts.getRelations().size(), parseErrors.size());
        return facts;
    }

    // ========== 文件处理 ==========

    private void processFile(String relPath, String moduleId) throws IOException {
        Path fullPath = repoRoot.resolve(relPath);
        if (!Files.exists(fullPath)) return;
        String sourceCode = Files.readString(fullPath, StandardCharsets.UTF_8);
        processFileFromContent(relPath, moduleId, sourceCode);
    }

    private void processFileFromContent(String relPath, String moduleId, String sourceCode) {

        // 创建 FILE 实体
        String fileId = IdGenerator.fileId(relPath);
        String fileName = relPath.contains("/") ? relPath.substring(relPath.lastIndexOf('/') + 1) : relPath;
        facts.addEntity(new StructureEntity(fileId, EntityType.FILE, fileName)
                .location(relPath).moduleId(moduleId).language("java"));

        // JavaParser 解析
        ParseResult<CompilationUnit> result = sharedParser.parse(sourceCode);
        if (!result.isSuccessful() || result.getResult().isEmpty()) {
            String errors = result.getProblems().stream()
                    .map(p -> p.getMessage())
                    .collect(Collectors.joining("; "));
            parseErrors.add(relPath + ": " + errors);
            reporter.fileError(relPath, errors);
            return;
        }

        CompilationUnit cu = result.getResult().get();

        // 提取包名
        String pkgName = cu.getPackageDeclaration()
                .map(pd -> pd.getNameAsString())
                .orElse("");
        packageByPath.put(relPath, pkgName);

        String pkgId = null;
        if (!pkgName.isEmpty() && !createdPackages.contains(pkgName)) {
            pkgId = IdGenerator.packageId(pkgName);
            facts.addEntity(new StructureEntity(pkgId, EntityType.PACKAGE, pkgName)
                    .language("java"));
            createdPackages.add(pkgName);
        } else if (!pkgName.isEmpty()) {
            pkgId = IdGenerator.packageId(pkgName);
        }

        // 收集 import 列表（供方法调用解析备用）
        List<String> imports = cu.getImports().stream()
                .map(imp -> imp.getNameAsString())
                .collect(Collectors.toList());

        // 遍历类型声明
        for (TypeDeclaration<?> typeDecl : cu.getTypes()) {
            processTypeDecl(typeDecl, relPath, moduleId, fileId, pkgId, pkgName, sourceCode, imports);
        }
    }

    // ========== 类型声明处理 ==========

    private void processTypeDecl(TypeDeclaration<?> typeDecl, String relPath, String moduleId,
                                  String fileId, String pkgId, String pkgName,
                                  String sourceCode, List<String> imports) {
        String name = typeDecl.getNameAsString();

        // 确定实体类型
        EntityType entityType;
        if (typeDecl instanceof EnumDeclaration) {
            entityType = EntityType.ENUM;
        } else if (typeDecl instanceof AnnotationDeclaration) {
            entityType = EntityType.ANNOTATION_TYPE;
        } else if (typeDecl instanceof ClassOrInterfaceDeclaration cid) {
            if (cid.isInterface()) {
                entityType = EntityType.INTERFACE;
            } else {
                entityType = EntityType.CLASS;
            }
        } else if (typeDecl instanceof RecordDeclaration) {
            entityType = EntityType.CLASS; // record 视为 class
        } else {
            return; // 未知类型，跳过
        }

        // 生成稳定 ID
        String classId = IdGenerator.classId(relPath, name);
        classIds.put(relPath + "|" + name, classId);

        // 位置信息
        String location = typeDecl.getBegin()
                .map(pos -> relPath + ":" + pos.line)
                .orElse(relPath);

        // 提取注解信息
        String feignTarget = extractFeignClientTarget(typeDecl);
        String mappingPath = extractMappingPath(typeDecl);
        List<String> modifiers = typeDecl.getModifiers().stream()
                .map(m -> m.getKeyword().asString())
                .collect(Collectors.toList());

        // 创建实体
        StructureEntity entity = new StructureEntity(classId, entityType, name)
                .location(location)
                .moduleId(moduleId)
                .language("java")
                .attr("visibility", modifiers)
                .attr("path", mappingPath != null ? mappingPath : "");

        if (feignTarget != null) {
            entity.attr("feign_target_service", feignTarget);
        }
        if (typeDecl instanceof RecordDeclaration) {
            entity.attr("is_record", true);
        }
        if (typeDecl instanceof ClassOrInterfaceDeclaration cid2 && cid2.isAbstract()) {
            entity.attr("is_abstract", true);
        }

        facts.addEntity(entity);

        // 关系
        if (pkgId != null) {
            facts.addRelation(new StructureRelation(RelationType.BELONGS_TO, classId, pkgId));
        }
        facts.addRelation(new StructureRelation(RelationType.RELATES_TO, classId, fileId));

        // 继承关系
        if (typeDecl instanceof ClassOrInterfaceDeclaration cid) {
            for (ClassOrInterfaceType ext : cid.getExtendedTypes()) {
                String parentId = IdGenerator.classRefId(relPath, ext.getNameAsString());
                facts.addRelation(new StructureRelation(RelationType.EXTENDS, classId, parentId));
            }
            for (ClassOrInterfaceType impl : cid.getImplementedTypes()) {
                String ifaceId = IdGenerator.classRefId(relPath, impl.getNameAsString());
                facts.addRelation(new StructureRelation(RelationType.IMPLEMENTS, classId, ifaceId));
            }
        }

        // 枚举常量
        if (typeDecl instanceof EnumDeclaration enumDecl) {
            for (EnumConstantDeclaration constant : enumDecl.getEntries()) {
                String constName = constant.getNameAsString();
                String fieldId = IdGenerator.fieldId(classId, constName);
                facts.addEntity(new StructureEntity(fieldId, EntityType.FIELD, constName)
                        .location(constant.getBegin().map(p -> relPath + ":" + p.line).orElse(relPath))
                        .moduleId(moduleId).language("java")
                        .attr("is_enum_constant", true)
                        .attr("class_name", name));
                facts.addRelation(new StructureRelation(RelationType.CONTAINS, classId, fieldId));
            }
        }

        // Record 组件作为字段
        if (typeDecl instanceof RecordDeclaration recordDecl) {
            for (Parameter param : recordDecl.getParameters()) {
                String fieldName = param.getNameAsString();
                String fieldId = IdGenerator.fieldId(classId, fieldName);
                facts.addEntity(new StructureEntity(fieldId, EntityType.FIELD, fieldName)
                        .location(location).moduleId(moduleId).language("java")
                        .attr("is_record_component", true)
                        .attr("class_name", name));
                facts.addRelation(new StructureRelation(RelationType.CONTAINS, classId, fieldId));
            }
        }

        // 普通类字段提取 (private/public/protected 实例字段和静态字段)
        for (FieldDeclaration field : typeDecl.getFields()) {
            for (VariableDeclarator var : field.getVariables()) {
                String fieldName = var.getNameAsString();
                String fieldId = IdGenerator.fieldId(classId, fieldName);
                String fieldLoc = field.getBegin()
                        .map(pos -> relPath + ":" + pos.line)
                        .orElse(relPath);

                StructureEntity fieldEntity = new StructureEntity(fieldId, EntityType.FIELD, fieldName)
                        .location(fieldLoc)
                        .moduleId(moduleId)
                        .language("java")
                        .attr("class_name", name)
                        .attr("type", var.getTypeAsString());

                // 标记可见性
                if (field.isPublic()) fieldEntity.attr("visibility", "public");
                else if (field.isProtected()) fieldEntity.attr("visibility", "protected");
                else if (field.isPrivate()) fieldEntity.attr("visibility", "private");
                else fieldEntity.attr("visibility", "package");

                // 标记 static / final
                if (field.isStatic()) fieldEntity.attr("is_static", true);
                if (field.isFinal()) fieldEntity.attr("is_final", true);

                // 提取字段注解中的中文描述
                // 优先级: @Schema(title/description) > @ApiModelProperty(value) > @Column(columnDefinition) > Javadoc
                String fieldComment = extractFieldComment(field);
                if (fieldComment != null && !fieldComment.isEmpty()) {
                    fieldEntity.attr("comment", fieldComment);
                }

                facts.addEntity(fieldEntity);
                facts.addRelation(new StructureRelation(RelationType.CONTAINS, classId, fieldId));
            }
        }

        // 处理方法
        for (MethodDeclaration method : typeDecl.getMethods()) {
            processMethod(method, classId, relPath, moduleId, sourceCode, pkgName, name, imports);
        }

        // 处理构造函数
        for (ConstructorDeclaration ctor : typeDecl.getConstructors()) {
            processConstructor(ctor, classId, relPath, moduleId, name);
        }
    }

    // ========== 方法处理 ==========

    private void processMethod(MethodDeclaration method, String classId, String relPath,
                                String moduleId, String sourceCode, String pkgName,
                                String className, List<String> imports) {
        // 方法签名
        String sig = buildSignature(method);
        String methodId = IdGenerator.methodId(classId, sig);
        methodIds.put(classId + "|" + sig, methodId);

        // 位置
        String location = method.getBegin()
                .map(pos -> relPath + ":" + pos.line)
                .orElse(relPath);

        // Getter/Setter 检测
        boolean isGetter = isTrivalGetter(method);
        boolean isSetter = isTrivalSetter(method);

        // 代码片段
        String snippet = extractSnippet(method, sourceCode);

        // REST 路径
        String mappingPath = extractMethodMappingPath(method);

        // 创建实体
        StructureEntity entity = new StructureEntity(methodId, EntityType.METHOD, method.getNameAsString())
                .location(location)
                .moduleId(moduleId)
                .language("java")
                .attr("signature", sig)
                .attr("class_name", className)
                .attr("is_getter", isGetter)
                .attr("is_setter", isSetter);

        if (mappingPath != null) {
            entity.attr("path", mappingPath);
        }
        if (snippet != null && !snippet.isEmpty()) {
            entity.attr("code_snippet", snippet);
        }

        facts.addEntity(entity);

        // 关系
        facts.addRelation(new StructureRelation(RelationType.CONTAINS, classId, methodId));
        facts.addRelation(new StructureRelation(RelationType.BELONGS_TO, methodId, classId));

        // 方法调用提取（SymbolSolver）
        extractMethodCalls(method, methodId, classId, className, pkgName, imports);
    }

    private void processConstructor(ConstructorDeclaration ctor, String classId,
                                     String relPath, String moduleId, String className) {
        String sig = "<init>(" + ctor.getParameters().stream()
                .map(p -> p.getTypeAsString())
                .collect(Collectors.joining(",")) + ")";
        String methodId = IdGenerator.methodId(classId, sig);

        String location = ctor.getBegin()
                .map(pos -> relPath + ":" + pos.line)
                .orElse(relPath);

        facts.addEntity(new StructureEntity(methodId, EntityType.METHOD, "<init>")
                .location(location).moduleId(moduleId).language("java")
                .attr("signature", sig).attr("class_name", className)
                .attr("is_getter", false).attr("is_setter", false));

        facts.addRelation(new StructureRelation(RelationType.CONTAINS, classId, methodId));
        facts.addRelation(new StructureRelation(RelationType.BELONGS_TO, methodId, classId));
    }

    // ========== 方法签名构建 ==========

    private String buildSignature(MethodDeclaration method) {
        String params = method.getParameters().stream()
                .map(p -> simplifyType(p.getTypeAsString()))
                .collect(Collectors.joining(","));
        return method.getNameAsString() + "(" + params + ")";
    }

    /** 简化类型名：去掉泛型参数，保持与 Python javalang 一致 */
    private String simplifyType(String type) {
        // List<String> → List, Map<K,V> → Map
        int idx = type.indexOf('<');
        if (idx > 0) type = type.substring(0, idx);
        // 去掉包前缀
        idx = type.lastIndexOf('.');
        if (idx > 0) type = type.substring(idx + 1);
        // 去掉数组标记的空格
        return type.replace(" ", "");
    }

    // ========== Getter/Setter 检测 ==========
    //
    // 双重判断策略：
    //   条件1（字段匹配）：方法名去掉 get/set/is 后，剩余部分在类的字段列表中能找到
    //   条件2（结构检测）：方法体是简单的 return field / this.field = param
    //   两个条件都满足才算 getter/setter
    //
    // 这样做的好处：
    //   - setMd5Param() → 类里没有 md5Param 字段 → 排除（即使 body 结构像 setter）
    //   - getName() 里有缓存逻辑 → body 结构不是简单 return → 排除（即使有 name 字段）
    //   - 两重保险，最大限度避免误判

    private boolean isTrivalGetter(MethodDeclaration method) {
        // 条件1：方法签名 — 无参数
        if (method.getParameters().size() != 0) return false;

        // 条件2：方法名匹配字段 — get/is 前缀 + 类里有对应字段
        if (!matchesClassField(method, "get", "is")) return false;

        // 条件3：方法体结构 — 只有 1 条 return 语句，返回字段
        if (!method.getBody().isPresent()) return false;
        var stmts = method.getBody().get().getStatements();
        if (stmts.size() != 1) return false;
        if (!stmts.get(0).isReturnStmt()) return false;
        var retExpr = stmts.get(0).asReturnStmt().getExpression();
        if (retExpr.isEmpty()) return false;
        Expression expr = retExpr.get();
        return expr.isNameExpr() || expr.isFieldAccessExpr();
    }

    private boolean isTrivalSetter(MethodDeclaration method) {
        // 条件1：方法签名 — 恰好 1 个参数
        if (method.getParameters().size() != 1) return false;

        // 条件2：方法名匹配字段 — set 前缀 + 类里有对应字段
        if (!matchesClassField(method, "set")) return false;

        // 条件3：方法体结构 — 最多 2 条语句（赋值 + 可选 return）
        if (!method.getBody().isPresent()) return false;
        var stmts = method.getBody().get().getStatements();
        if (stmts.size() < 1 || stmts.size() > 2) return false;

        boolean hasAssign = false;
        for (var stmt : stmts) {
            if (stmt.isExpressionStmt()) {
                Expression expr = stmt.asExpressionStmt().getExpression();
                if (expr.isAssignExpr()) {
                    AssignExpr assign = expr.asAssignExpr();
                    if (assign.getTarget().isFieldAccessExpr() || assign.getTarget().isNameExpr()) {
                        hasAssign = true;
                    } else {
                        return false;
                    }
                } else {
                    return false; // 方法调用等非赋值语句 → 不是 setter
                }
            } else if (stmt.isReturnStmt()) {
                continue; // return this; 或 return; 允许
            } else {
                return false; // if/for/变量声明等 → 不是 setter
            }
        }
        return hasAssign;
    }

    /**
     * 检查方法名去掉 prefix 后，是否匹配类中的某个字段名。
     * 使用 Java Bean 规范 (Introspector.decapitalize) 反推字段名。
     *
     * Java Bean 命名规则：
     *   getSted()  → 去掉get → "Sted"  → 前两位 S大写 t小写 → 首字母小写 → "sted"
     *   getPWD()   → 去掉get → "PWD"   → 前两位 P,W 都大写 → 保持原样  → "PWD"
     *   getsPP()   → 去掉get → "sPP"   → 首字母已小写     → 保持原样  → "sPP"
     *   getRMB()   → 去掉get → "RMB"   → 前两位 R,M 都大写 → 保持原样  → "RMB"
     *   isEmpty()  → 去掉is  → "Empty" → 前两位 E大写 m小写 → 首字母小写 → "empty"
     */
    private boolean matchesClassField(MethodDeclaration method, String... prefixes) {
        String methodName = method.getNameAsString();

        // 从方法名提取候选字段名
        String candidateField = null;
        for (String prefix : prefixes) {
            if (methodName.length() > prefix.length() && methodName.startsWith(prefix)) {
                String remainder = methodName.substring(prefix.length());
                if (remainder.isEmpty()) continue;

                // Java Bean 规范: Introspector.decapitalize()
                candidateField = beanDecapitalize(remainder);
                break;
            }
        }
        if (candidateField == null || candidateField.isEmpty()) return false;

        // 在所属类中查找字段
        TypeDeclaration<?> parentType = method.findAncestor(TypeDeclaration.class).orElse(null);
        if (parentType == null) return false;

        for (FieldDeclaration field : parentType.getFields()) {
            for (VariableDeclarator var : field.getVariables()) {
                if (var.getNameAsString().equals(candidateField)) {
                    return true; // 找到匹配字段
                }
            }
        }
        return false; // 类里没有这个字段
    }

    /**
     * 等价于 java.beans.Introspector.decapitalize()。
     * 从 getter/setter 方法名的剩余部分反推字段名。
     *
     * 规则：
     * - 前两个字符都是大写 → 保持原样 ("PWD" → "PWD", "RMB" → "RMB")
     * - 否则 → 首字母小写 ("Sted" → "sted", "Name" → "name")
     * - 首字母已经小写 → 保持原样 ("sPP" → "sPP")
     */
    private static String beanDecapitalize(String name) {
        if (name == null || name.isEmpty()) return name;
        // 前两个字符都大写 → 保持原样（Java Bean 规范）
        if (name.length() > 1
                && Character.isUpperCase(name.charAt(0))
                && Character.isUpperCase(name.charAt(1))) {
            return name;
        }
        // 否则首字母小写
        return Character.toLowerCase(name.charAt(0)) + name.substring(1);
    }

    // ========== 字段注释提取 ==========

    /**
     * 从字段的注解或 Javadoc 中提取中文描述。
     * 优先级:
     *   1. @Schema(title="...") 或 @Schema(description="...")  — Swagger/SpringDoc
     *   2. @ApiModelProperty(value="...") 或 @ApiModelProperty("...")  — Swagger 2.x
     *   3. Javadoc 注释第一行
     */
    private String extractFieldComment(FieldDeclaration field) {
        // 1. @Schema 注解
        for (var ann : field.getAnnotations()) {
            String annName = ann.getNameAsString();
            if ("Schema".equals(annName) || "io.swagger.v3.oas.annotations.media.Schema".equals(annName)) {
                String val = extractAnnotationStringAttr(ann, "title");
                if (val == null || val.isEmpty()) val = extractAnnotationStringAttr(ann, "description");
                if (val != null && !val.isEmpty()) return val;
            }
        }

        // 2. @ApiModelProperty 注解
        for (var ann : field.getAnnotations()) {
            String annName = ann.getNameAsString();
            if ("ApiModelProperty".equals(annName)) {
                String val = extractAnnotationStringAttr(ann, "value");
                // @ApiModelProperty("xxx") 的简写形式
                if ((val == null || val.isEmpty()) && ann.isSingleMemberAnnotationExpr()) {
                    Expression expr = ann.asSingleMemberAnnotationExpr().getMemberValue();
                    if (expr.isStringLiteralExpr()) {
                        val = expr.asStringLiteralExpr().getValue();
                    }
                }
                if (val != null && !val.isEmpty()) return val;
            }
        }

        // 3. Javadoc
        if (field.getJavadoc().isPresent()) {
            String doc = field.getJavadoc().get().getDescription().toText().trim();
            // 取第一行
            int newline = doc.indexOf('\n');
            if (newline > 0) doc = doc.substring(0, newline).trim();
            if (!doc.isEmpty()) return doc;
        }

        return null;
    }

    /**
     * 从注解中提取指定名称的 String 属性值。
     * 如 @Schema(title="订单状态") → 传入 "title" 返回 "订单状态"
     */
    private String extractAnnotationStringAttr(com.github.javaparser.ast.expr.AnnotationExpr ann, String attrName) {
        if (ann.isNormalAnnotationExpr()) {
            for (var pair : ann.asNormalAnnotationExpr().getPairs()) {
                if (attrName.equals(pair.getNameAsString())) {
                    Expression val = pair.getValue();
                    if (val.isStringLiteralExpr()) {
                        return val.asStringLiteralExpr().getValue();
                    }
                }
            }
        }
        if (ann.isSingleMemberAnnotationExpr() && "value".equals(attrName)) {
            Expression val = ann.asSingleMemberAnnotationExpr().getMemberValue();
            if (val.isStringLiteralExpr()) {
                return val.asStringLiteralExpr().getValue();
            }
        }
        return null;
    }

    // ========== 代码片段提取 ==========

    private String extractSnippet(MethodDeclaration method, String sourceCode) {
        try {
            int begin = method.getBegin().map(p -> p.line).orElse(-1);
            int end = method.getEnd().map(p -> p.line).orElse(-1);
            if (begin < 1 || end < begin) return null;

            String[] lines = sourceCode.split("\n");
            StringBuilder sb = new StringBuilder();
            for (int i = begin - 1; i < Math.min(end, lines.length); i++) {
                sb.append(lines[i]).append("\n");
            }
            String snippet = sb.toString().trim();
            if (snippet.length() > MAX_SNIPPET) {
                snippet = snippet.substring(0, MAX_SNIPPET) + "...";
            }
            return snippet;
        } catch (Exception e) {
            return null;
        }
    }

    // ========== 注解提取 ==========

    private static final Set<String> MAPPING_ANNOTATIONS = Set.of(
            "RequestMapping", "GetMapping", "PostMapping", "PutMapping",
            "DeleteMapping", "PatchMapping"
    );

    private String extractMappingPath(TypeDeclaration<?> typeDecl) {
        for (AnnotationExpr ann : typeDecl.getAnnotations()) {
            String annName = ann.getNameAsString();
            if (MAPPING_ANNOTATIONS.contains(annName)) {
                return extractAnnotationValue(ann);
            }
        }
        return null;
    }

    private String extractMethodMappingPath(MethodDeclaration method) {
        for (AnnotationExpr ann : method.getAnnotations()) {
            String annName = ann.getNameAsString();
            if (MAPPING_ANNOTATIONS.contains(annName)) {
                return extractAnnotationValue(ann);
            }
        }
        return null;
    }

    private String extractFeignClientTarget(TypeDeclaration<?> typeDecl) {
        for (AnnotationExpr ann : typeDecl.getAnnotations()) {
            if ("FeignClient".equals(ann.getNameAsString())) {
                // @FeignClient(name="xxx") 或 @FeignClient("xxx")
                return extractAnnotationNameOrValue(ann);
            }
        }
        return null;
    }

    private String extractAnnotationValue(AnnotationExpr ann) {
        if (ann.isSingleMemberAnnotationExpr()) {
            return cleanStringLiteral(ann.asSingleMemberAnnotationExpr().getMemberValue().toString());
        }
        if (ann.isNormalAnnotationExpr()) {
            for (var pair : ann.asNormalAnnotationExpr().getPairs()) {
                if ("value".equals(pair.getNameAsString()) || "path".equals(pair.getNameAsString())) {
                    return cleanStringLiteral(pair.getValue().toString());
                }
            }
        }
        return null;
    }

    private String extractAnnotationNameOrValue(AnnotationExpr ann) {
        if (ann.isSingleMemberAnnotationExpr()) {
            return cleanStringLiteral(ann.asSingleMemberAnnotationExpr().getMemberValue().toString());
        }
        if (ann.isNormalAnnotationExpr()) {
            for (var pair : ann.asNormalAnnotationExpr().getPairs()) {
                String pairName = pair.getNameAsString();
                if ("name".equals(pairName) || "value".equals(pairName)) {
                    return cleanStringLiteral(pair.getValue().toString());
                }
            }
        }
        return null;
    }

    private String cleanStringLiteral(String s) {
        if (s == null) return null;
        s = s.trim();
        if (s.startsWith("\"") && s.endsWith("\"")) {
            s = s.substring(1, s.length() - 1);
        }
        return s;
    }

    // ========== 方法调用提取 (SymbolSolver) ==========

    private void extractMethodCalls(MethodDeclaration method, String methodId, String classId,
                                     String className, String pkgName, List<String> imports) {
        if (!method.getBody().isPresent()) return;

        method.getBody().get().findAll(MethodCallExpr.class).forEach(callExpr -> {
            try {
                // 尝试 SymbolSolver 精确解析
                ResolvedMethodDeclaration resolved = callExpr.resolve();
                String targetFqn = resolved.declaringType().getQualifiedName();
                String targetMethod = resolved.getName();
                deferredCalls.add(new DeferredCall(methodId, classId, targetFqn, targetMethod));
            } catch (Exception e) {
                // SymbolSolver 解析失败（第三方库/未导入），用 fallback
                String calleeName = callExpr.getNameAsString();
                String targetFqn = null;

                if (callExpr.getScope().isPresent()) {
                    Expression scope = callExpr.getScope().get();
                    if (scope.isNameExpr()) {
                        String scopeName = scope.asNameExpr().getNameAsString();
                        // 首字母大写 → 可能是类名（静态调用）
                        if (Character.isUpperCase(scopeName.charAt(0))) {
                            targetFqn = resolveSimpleType(scopeName, imports, pkgName);
                        } else {
                            // 小写 → 字段/变量调用，尝试推断类型
                            targetFqn = inferFieldType(scopeName, method, imports, pkgName);
                        }
                    } else if (scope.isThisExpr()) {
                        targetFqn = pkgName.isEmpty() ? className : pkgName + "." + className;
                    }
                } else {
                    // 无 scope → 本类方法
                    targetFqn = pkgName.isEmpty() ? className : pkgName + "." + className;
                }

                if (targetFqn != null) {
                    deferredCalls.add(new DeferredCall(methodId, classId, targetFqn, calleeName));
                }
            }
        });
    }

    private String resolveSimpleType(String simpleName, List<String> imports, String pkgName) {
        for (String imp : imports) {
            if (imp.endsWith("." + simpleName)) return imp;
        }
        return pkgName.isEmpty() ? simpleName : pkgName + "." + simpleName;
    }

    private String inferFieldType(String fieldName, MethodDeclaration method, List<String> imports, String pkgName) {
        // 简单推断：从方法所在类的字段声明中查找类型
        // JavaParser AST 可以直接访问父类的字段
        TypeDeclaration<?> parentType = method.findAncestor(TypeDeclaration.class).orElse(null);
        if (parentType != null) {
            for (FieldDeclaration field : parentType.getFields()) {
                for (VariableDeclarator var : field.getVariables()) {
                    if (var.getNameAsString().equals(fieldName)) {
                        String typeName = simplifyType(var.getTypeAsString());
                        return resolveSimpleType(typeName, imports, pkgName);
                    }
                }
            }
        }
        return null;
    }

    // ========== 延迟调用解析 ==========

    private void resolveDeferredCalls() {
        // 构建 FQN → classId 索引
        Map<String, String> fqnToClassId = new HashMap<>();
        for (var entry : classIds.entrySet()) {
            String key = entry.getKey(); // "relPath|className"
            String cId = entry.getValue();
            String relPath = key.substring(0, key.lastIndexOf('|'));
            String className = key.substring(key.lastIndexOf('|') + 1);
            String pkg = packageByPath.getOrDefault(relPath, "");
            String fqn = pkg.isEmpty() ? className : pkg + "." + className;
            fqnToClassId.put(fqn, cId);
        }

        // 构建 classId → {methodName → [methodIds]} 索引
        Map<String, Map<String, List<String>>> methodsByClass = new HashMap<>();
        for (var entry : methodIds.entrySet()) {
            String key = entry.getKey(); // "classId|signature"
            String mId = entry.getValue();
            int sep = key.indexOf('|');
            String cId = key.substring(0, sep);
            String sig = key.substring(sep + 1);
            String mName = sig.contains("(") ? sig.substring(0, sig.indexOf('(')) : sig;
            methodsByClass.computeIfAbsent(cId, k -> new HashMap<>())
                    .computeIfAbsent(mName, k -> new ArrayList<>())
                    .add(mId);
        }

        // 解析
        Set<String> seenEdges = new HashSet<>();
        for (DeferredCall call : deferredCalls) {
            String targetClassId = fqnToClassId.get(call.targetFqn);
            if (targetClassId == null) continue;

            var methods = methodsByClass.getOrDefault(targetClassId, Map.of());
            var mIds = methods.get(call.calleeMethod);
            if (mIds == null || mIds.isEmpty()) continue;

            String calleeId = mIds.get(0); // 取第一个（重载暂不区分）
            if (calleeId.equals(call.callerMethodId)) continue; // 自调用
            String edgeKey = call.callerMethodId + "|" + calleeId;
            if (seenEdges.contains(edgeKey)) continue;
            seenEdges.add(edgeKey);

            StructureRelation rel = new StructureRelation(RelationType.CALLS, call.callerMethodId, calleeId);
            // 查找名称
            StructureEntity callerEntity = facts.findEntityById(call.callerMethodId);
            StructureEntity calleeEntity = facts.findEntityById(calleeId);
            if (callerEntity != null) {
                rel.attr("caller_class", String.valueOf(callerEntity.getAttributes().get("class_name")));
                rel.attr("caller_method", callerEntity.getName());
            }
            if (calleeEntity != null) {
                rel.attr("callee_class", String.valueOf(calleeEntity.getAttributes().get("class_name")));
                rel.attr("callee_method", calleeEntity.getName());
            }
            facts.addRelation(rel);
        }
    }

    // ========== 跨服务边提取 ==========

    private void extractCrossServiceEdges() {
        Set<String> createdServices = new HashSet<>();

        // 创建 SERVICE 实体
        for (ModuleConfig.Module mod : config.getModules()) {
            String svcId = IdGenerator.serviceId(mod.getId());
            if (!createdServices.contains(svcId)) {
                facts.addEntity(new StructureEntity(svcId, EntityType.SERVICE, mod.getId())
                        .moduleId(mod.getId()).language("java"));
                createdServices.add(svcId);
            }
        }

        // CLASS/INTERFACE → BELONGS_TO → SERVICE
        for (StructureEntity e : new ArrayList<>(facts.getEntities())) {
            if ((e.getType() == EntityType.CLASS || e.getType() == EntityType.INTERFACE
                    || e.getType() == EntityType.ENUM || e.getType() == EntityType.ANNOTATION_TYPE)
                    && e.getModuleId() != null) {
                String svcId = IdGenerator.serviceId(e.getModuleId());
                if (createdServices.contains(svcId)) {
                    facts.addRelation(new StructureRelation(RelationType.BELONGS_TO, e.getId(), svcId));
                }
            }
        }

        // API_ENDPOINT 实体
        for (StructureEntity e : new ArrayList<>(facts.getEntities())) {
            if (e.getType() == EntityType.METHOD) {
                String path = (String) e.getAttributes().get("path");
                if (path != null && !path.isEmpty()) {
                    String apiId = IdGenerator.apiEndpointId(e.getId());
                    facts.addEntity(new StructureEntity(apiId, EntityType.API_ENDPOINT, path)
                            .moduleId(e.getModuleId()).language("java")
                            .attr("method_entity_id", e.getId())
                            .attr("class_name", e.getAttributes().get("class_name"))
                            .attr("method_name", e.getName()));

                    String svcId = IdGenerator.serviceId(e.getModuleId());
                    if (createdServices.contains(svcId)) {
                        facts.addRelation(new StructureRelation(RelationType.SERVICE_EXPOSES, svcId, apiId));
                    }
                }
            }
        }
    }

    private void extractFeignBindings() {
        Set<String> existingServices = new HashSet<>();
        for (StructureEntity e : facts.getEntities()) {
            if (e.getType() == EntityType.SERVICE) {
                existingServices.add(e.getId());
            }
        }

        for (StructureEntity e : new ArrayList<>(facts.getEntities())) {
            if (e.getType() != EntityType.CLASS && e.getType() != EntityType.INTERFACE) continue;
            String target = (String) e.getAttributes().get("feign_target_service");
            if (target == null || target.isEmpty()) continue;

            String targetSvcId = IdGenerator.serviceId(target);
            // 创建目标 SERVICE（如果不存在）
            if (!existingServices.contains(targetSvcId)) {
                facts.addEntity(new StructureEntity(targetSvcId, EntityType.SERVICE, target)
                        .moduleId(target).language("java"));
                existingServices.add(targetSvcId);
            }

            facts.addRelation(new StructureRelation(RelationType.BINDS_TO_SERVICE, e.getId(), targetSvcId));

            // SERVICE_CALLS
            if (e.getModuleId() != null) {
                String sourceSvcId = IdGenerator.serviceId(e.getModuleId());
                if (existingServices.contains(sourceSvcId) && !sourceSvcId.equals(targetSvcId)) {
                    facts.addRelation(new StructureRelation(RelationType.SERVICE_CALLS, sourceSvcId, targetSvcId));
                }
            }
        }
    }

    // ========== 关系属性填充 ==========

    private void enrichRelationAttributes() {
        // 预建 ID → Entity 索引（O(1) 查找，替代 O(N) 线性扫描）
        Map<String, StructureEntity> entityIndex = new HashMap<>();
        for (StructureEntity e : facts.getEntities()) {
            entityIndex.put(e.getId(), e);
        }

        for (StructureRelation r : facts.getRelations()) {
            StructureEntity src = entityIndex.get(r.getSourceId());
            StructureEntity tgt = entityIndex.get(r.getTargetId());
            r.attr("source_name", src != null ? src.getName() : r.getSourceId());
            r.attr("target_name", tgt != null ? tgt.getName() : r.getTargetId());
            r.attr("source_type", src != null ? src.getType().getValue() : "unknown");
            r.attr("target_type", tgt != null ? tgt.getType().getValue() : "unknown");
        }
    }

    // ========== 内部类 ==========

    private record DeferredCall(String callerMethodId, String callerClassId, String targetFqn, String calleeMethod) {}
}
