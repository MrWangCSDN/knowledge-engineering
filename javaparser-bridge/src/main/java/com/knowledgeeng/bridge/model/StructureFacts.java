package com.knowledgeeng.bridge.model;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.*;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 顶层输出模型 — 与 Python StructureFacts 完全一致。
 * 这是 Java→Python 的唯一数据契约。
 * 线程安全：支持多线程并发添加实体和关系。
 */
public class StructureFacts {

    @JsonProperty("entities")
    private List<StructureEntity> entities = Collections.synchronizedList(new ArrayList<>());

    @JsonProperty("relations")
    private List<StructureRelation> relations = Collections.synchronizedList(new ArrayList<>());

    @JsonProperty("meta")
    private Map<String, Object> meta = Collections.synchronizedMap(new HashMap<>());

    public StructureFacts() {}

    public void addEntity(StructureEntity entity) {
        this.entities.add(entity);
    }

    public void addRelation(StructureRelation relation) {
        this.relations.add(relation);
    }

    public void setMeta(String key, Object value) {
        this.meta.put(key, value);
    }

    // Getters
    public List<StructureEntity> getEntities() { return entities; }
    public List<StructureRelation> getRelations() { return relations; }
    public Map<String, Object> getMeta() { return meta; }

    /** 按 ID 查找实体 */
    public StructureEntity findEntityById(String id) {
        return entities.stream().filter(e -> id.equals(e.getId())).findFirst().orElse(null);
    }
}
