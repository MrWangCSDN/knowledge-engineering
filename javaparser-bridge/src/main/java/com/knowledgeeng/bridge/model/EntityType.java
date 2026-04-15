package com.knowledgeeng.bridge.model;

import com.fasterxml.jackson.annotation.JsonValue;

/**
 * 实体类型枚举 — 与 Python EntityType 完全一致。
 * JSON 序列化为小写字符串。
 */
public enum EntityType {
    FILE("file"),
    MODULE("module"),
    PACKAGE("package"),
    CLASS("class"),
    INTERFACE("interface"),
    ENUM("enum"),
    ANNOTATION_TYPE("annotation_type"),
    METHOD("method"),
    FIELD("field"),
    PARAMETER("parameter"),
    SERVICE("service"),
    API_ENDPOINT("api_endpoint");

    private final String value;

    EntityType(String value) {
        this.value = value;
    }

    @JsonValue
    public String getValue() {
        return value;
    }
}
