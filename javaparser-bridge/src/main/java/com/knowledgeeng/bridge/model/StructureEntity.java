package com.knowledgeeng.bridge.model;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.HashMap;
import java.util.Map;

/**
 * 结构实体 — 与 Python StructureEntity 完全一致。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public class StructureEntity {

    @JsonProperty("id")
    private String id;

    @JsonProperty("type")
    private EntityType type;

    @JsonProperty("name")
    private String name;

    @JsonProperty("location")
    private String location;

    @JsonProperty("module_id")
    private String moduleId;

    @JsonProperty("language")
    private String language;

    @JsonProperty("attributes")
    private Map<String, Object> attributes = new HashMap<>();

    public StructureEntity() {}

    public StructureEntity(String id, EntityType type, String name) {
        this.id = id;
        this.type = type;
        this.name = name;
    }

    // Builder-style setters
    public StructureEntity id(String id) { this.id = id; return this; }
    public StructureEntity type(EntityType type) { this.type = type; return this; }
    public StructureEntity name(String name) { this.name = name; return this; }
    public StructureEntity location(String location) { this.location = location; return this; }
    public StructureEntity moduleId(String moduleId) { this.moduleId = moduleId; return this; }
    public StructureEntity language(String language) { this.language = language; return this; }
    public StructureEntity attr(String key, Object value) { this.attributes.put(key, value); return this; }

    // Getters
    public String getId() { return id; }
    public EntityType getType() { return type; }
    public String getName() { return name; }
    public String getLocation() { return location; }
    public String getModuleId() { return moduleId; }
    public String getLanguage() { return language; }
    public Map<String, Object> getAttributes() { return attributes; }
}
