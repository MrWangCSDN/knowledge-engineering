// javaparser-bridge/src/test/java/com/knowledgeeng/bridge/extract/AnnotationCollectTest.java
package com.knowledgeeng.bridge.extract;

import static org.junit.jupiter.api.Assertions.assertTrue;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import java.util.List;
import org.junit.jupiter.api.Test;

/** 验证 collectAnnotationNames 能取出类上 applied 的注解简单名。 */
class AnnotationCollectTest {
    @Test
    void collectsClassAnnotationSimpleNames() {
        CompilationUnit cu = StaticJavaParser.parse(
            "@Controller @RequestMapping(\"/x\") class Foo {}");
        ClassOrInterfaceDeclaration cls =
            cu.findFirst(ClassOrInterfaceDeclaration.class).orElseThrow();
        List<String> names = JavaFileProcessor.collectAnnotationNames(cls);
        assertTrue(names.contains("Controller"));
        assertTrue(names.contains("RequestMapping"));
    }
}
