package com.knowledgeeng.bridge.config;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.*;

/**
 * Python 传入的模块配置 JSON 反序列化模型。
 * 由 Python javaparser_bridge.py 生成临时 JSON 文件。
 */
public class ModuleConfig {

    @JsonProperty("modules")
    private List<Module> modules = new ArrayList<>();

    /** 文件相对路径 → module_id 映射（Python 预计算） */
    @JsonProperty("file_module_map")
    private Map<String, String> fileModuleMap = new HashMap<>();

    @JsonProperty("repo_path")
    private String repoPath;

    @JsonProperty("extract_cross_service")
    private boolean extractCrossService = true;

    @JsonProperty("java_source_extensions")
    private List<String> javaSourceExtensions = List.of(".java");

    public List<Module> getModules() { return modules; }
    public Map<String, String> getFileModuleMap() { return fileModuleMap; }
    public String getRepoPath() { return repoPath; }
    public boolean isExtractCrossService() { return extractCrossService; }
    public List<String> getJavaSourceExtensions() { return javaSourceExtensions; }

    public String getModuleId(String relativePath) {
        return fileModuleMap.getOrDefault(relativePath, null);
    }

    public static class Module {
        @JsonProperty("id")
        private String id;
        @JsonProperty("name")
        private String name;
        @JsonProperty("path")
        private String path;
        @JsonProperty("business_domains")
        private List<String> businessDomains = new ArrayList<>();

        public String getId() { return id; }
        public String getName() { return name; }
        public String getPath() { return path; }
        public List<String> getBusinessDomains() { return businessDomains; }
    }
}
