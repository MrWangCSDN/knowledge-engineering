package com.knowledgeeng.bridge;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.knowledgeeng.bridge.config.ModuleConfig;
import com.knowledgeeng.bridge.extract.JavaFileProcessor;
import com.knowledgeeng.bridge.model.StructureFacts;
import com.knowledgeeng.bridge.progress.ProgressReporter;
import picocli.CommandLine;
import picocli.CommandLine.Command;
import picocli.CommandLine.Option;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.Callable;

/**
 * JavaParser Bridge CLI — 解析 Java 代码，输出 StructureFacts JSON。
 *
 * 用法:
 *   java -jar javaparser-bridge-1.0.0-shaded.jar \
 *       --repo-path /path/to/repo \
 *       --modules-json /tmp/modules.json \
 *       --output /tmp/structure_facts.json
 */
@Command(name = "javaparser-bridge", version = "1.0.0",
         description = "Parse Java source code and output StructureFacts JSON")
public class Main implements Callable<Integer> {

    @Option(names = {"--repo-path", "-r"}, required = true, description = "Repository root directory")
    private File repoPath;

    @Option(names = {"--modules-json", "-m"}, required = true, description = "Module configuration JSON file")
    private File modulesJson;

    @Option(names = {"--output", "-o"}, description = "Output JSON file path (default: stdout)")
    private File output;

    @Override
    public Integer call() throws Exception {
        ProgressReporter reporter = new ProgressReporter();
        ObjectMapper mapper = new ObjectMapper();

        // 读取模块配置
        reporter.progress(0, 1, "Loading configuration...");
        ModuleConfig config = mapper.readValue(modulesJson, ModuleConfig.class);

        // 执行解析
        Path root = repoPath.toPath().toAbsolutePath();
        JavaFileProcessor processor = new JavaFileProcessor(root, config, reporter);
        StructureFacts facts = processor.extractAll();

        // 输出 JSON
        ObjectMapper writer = new ObjectMapper();
        writer.configure(SerializationFeature.INDENT_OUTPUT, false);

        if (output != null) {
            writer.writeValue(output, facts);
            reporter.progress(1, 1, "Output written to " + output.getAbsolutePath());
        } else {
            System.out.println(writer.writeValueAsString(facts));
        }

        return 0;
    }

    public static void main(String[] args) {
        int exitCode = new CommandLine(new Main()).execute(args);
        System.exit(exitCode);
    }
}
