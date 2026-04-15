package com.knowledgeeng.bridge.extract;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/**
 * 稳定实体 ID 生成器 — 与 Python _stable_id() 完全一致。
 *
 * Python 原始实现 (java_parser.py line 36-48):
 * <pre>
 * def _stable_id(prefix: str, *parts: str) -> str:
 *     raw = "|".join(str(p) for p in parts)
 *     h = hashlib.sha256(raw.encode()).hexdigest()[:12]
 *     return f"{prefix}//{h}"
 * </pre>
 *
 * 关键约束：
 * 1. 用 "|" 连接 parts
 * 2. UTF-8 编码
 * 3. SHA-256 哈希
 * 4. 取前 12 位小写 hex
 * 5. 格式: {prefix}//{hex12}
 * 6. FILE 实体特殊: file://{relative_path} (不哈希)
 */
public final class IdGenerator {

    private static final MessageDigest SHA256;

    static {
        try {
            SHA256 = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new RuntimeException("SHA-256 not available", e);
        }
    }

    private IdGenerator() {}

    /**
     * 生成稳定实体 ID。
     * 与 Python _stable_id(prefix, *parts) 完全一致。
     *
     * @param prefix 前缀 (class, method, package, field, class_ref, interface, service 等)
     * @param parts  哈希输入部分
     * @return {prefix}//{sha256_hex_12}
     */
    public static String stableId(String prefix, String... parts) {
        String raw = String.join("|", parts);
        byte[] hash;
        // MessageDigest 不是线程安全的，clone 或 synchronized
        synchronized (SHA256) {
            SHA256.reset();
            hash = SHA256.digest(raw.getBytes(StandardCharsets.UTF_8));
        }
        String hex = bytesToHex(hash).substring(0, 12);
        return prefix + "//" + hex;
    }

    /**
     * FILE 实体 ID — 不哈希，直接用相对路径。
     * 与 Python: file_id = f"file://{fitem.path}" 一致。
     */
    public static String fileId(String relativePath) {
        return "file://" + relativePath;
    }

    /**
     * PACKAGE 实体 ID。
     * Python: _stable_id("package", package_name)
     */
    public static String packageId(String packageName) {
        return stableId("package", packageName);
    }

    /**
     * CLASS/INTERFACE/ENUM/ANNOTATION 实体 ID。
     * Python: _stable_id("class", fitem.path, name)
     */
    public static String classId(String relativePath, String className) {
        return stableId("class", relativePath, className);
    }

    /**
     * METHOD 实体 ID。
     * Python: _stable_id("method", class_id, sig)
     * 注意：class_id 是完整的 "class//xxxxxx" 字符串。
     */
    public static String methodId(String classId, String methodSignature) {
        return stableId("method", classId, methodSignature);
    }

    /**
     * FIELD 实体 ID。
     * Python: _stable_id("field", class_id, field_name)
     */
    public static String fieldId(String classId, String fieldName) {
        return stableId("field", classId, fieldName);
    }

    /**
     * 被引用的类 ID（extends/implements 目标，可能不在本项目中）。
     * Python: _stable_id("class_ref", fitem.path, ext.name)
     */
    public static String classRefId(String relativePath, String refName) {
        return stableId("class_ref", relativePath, refName);
    }

    /**
     * SERVICE 实体 ID。
     * Python: f"service://{module_id}"
     */
    public static String serviceId(String moduleId) {
        return "service://" + moduleId;
    }

    /**
     * API_ENDPOINT 实体 ID。
     * Python: f"{method_id}#api"
     */
    public static String apiEndpointId(String methodId) {
        return methodId + "#api";
    }

    // --- Internal ---

    private static String bytesToHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            sb.append(String.format("%02x", b & 0xff));
        }
        return sb.toString();
    }
}
