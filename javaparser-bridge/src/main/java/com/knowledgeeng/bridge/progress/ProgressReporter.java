package com.knowledgeeng.bridge.progress;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.PrintStream;
import java.util.*;

/**
 * NDJSON 进度报告 — 输出到 stderr，Python 实时读取。
 */
public class ProgressReporter {

    private final PrintStream out;
    private final ObjectMapper mapper = new ObjectMapper();

    public ProgressReporter() {
        this(System.err);
    }

    public ProgressReporter(PrintStream out) {
        this.out = out;
    }

    public void progress(int current, int total, String message) {
        write(Map.of("type", "progress", "current", current, "total", total, "message", message));
    }

    public void fileError(String file, String error) {
        write(Map.of("type", "file_error", "file", file, "error", error));
    }

    public void fileDone(String file, int entities, int relations) {
        write(Map.of("type", "file_done", "file", file, "entities", entities, "relations", relations));
    }

    public void done(int totalEntities, int totalRelations, int errors) {
        write(Map.of("type", "done", "entities", totalEntities, "relations", totalRelations, "errors", errors));
    }

    private void write(Map<String, Object> data) {
        try {
            out.println(mapper.writeValueAsString(data));
            out.flush();
        } catch (Exception ignored) {}
    }
}
