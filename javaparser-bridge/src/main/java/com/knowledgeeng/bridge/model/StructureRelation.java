package com.knowledgeeng.bridge.model;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.HashMap;
import java.util.Map;

/**
 * 结构关系 — 与 Python StructureRelation 完全一致。
 */
public class StructureRelation {

    @JsonProperty("type")
    private RelationType type;

    @JsonProperty("source_id")
    private String sourceId;

    @JsonProperty("target_id")
    private String targetId;

    @JsonProperty("attributes")
    private Map<String, Object> attributes = new HashMap<>();

    public StructureRelation() {}

    public StructureRelation(RelationType type, String sourceId, String targetId) {
        this.type = type;
        this.sourceId = sourceId;
        this.targetId = targetId;
    }

    public StructureRelation attr(String key, Object value) {
        this.attributes.put(key, value);
        return this;
    }

    // Getters
    public RelationType getType() { return type; }
    public String getSourceId() { return sourceId; }
    public String getTargetId() { return targetId; }
    public Map<String, Object> getAttributes() { return attributes; }
}
