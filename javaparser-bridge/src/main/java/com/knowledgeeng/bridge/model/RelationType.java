package com.knowledgeeng.bridge.model;

import com.fasterxml.jackson.annotation.JsonValue;

/**
 * 关系类型枚举 — 与 Python RelationType 完全一致。
 */
public enum RelationType {
    CONTAINS("contains"),
    CALLS("calls"),
    EXTENDS("extends"),
    IMPLEMENTS("implements"),
    DEPENDS_ON("depends_on"),
    BELONGS_TO("belongs_to"),
    RELATES_TO("relates_to"),
    ANNOTATED_BY("annotated_by"),
    SERVICE_CALLS("service_calls"),
    SERVICE_EXPOSES("service_exposes"),
    BINDS_TO_SERVICE("binds_to_service");

    private final String value;

    RelationType(String value) {
        this.value = value;
    }

    @JsonValue
    public String getValue() {
        return value;
    }
}
