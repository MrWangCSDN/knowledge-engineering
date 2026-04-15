package com.knowledgeeng.bridge.extract;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * 交叉验证测试：Java IdGenerator 必须与 Python _stable_id 完全一致。
 *
 * 预期值由 Python 生成:
 * <pre>
 * from src.structure.java_parser import _stable_id
 * print(_stable_id("class", "mall-admin/src/main/java/com/mall/admin/service/ProductService.java", "ProductService"))
 * </pre>
 */
class IdGeneratorTest {

    @Test
    void testFileId() {
        // file:// 不哈希，直接拼路径
        assertEquals(
            "file://mall-admin/src/main/java/com/mall/admin/service/ProductService.java",
            IdGenerator.fileId("mall-admin/src/main/java/com/mall/admin/service/ProductService.java")
        );
    }

    @Test
    void testPackageId() {
        // _stable_id("package", "com.mall.admin.service")
        String id = IdGenerator.packageId("com.mall.admin.service");
        assertTrue(id.startsWith("package//"), "Should start with package//");
        assertEquals(21, id.length(), "package// (9) + 12 hex = 21 chars");
        // 验证格式: package//{12位hex}
        assertTrue(id.matches("package//[0-9a-f]{12}"));
    }

    @Test
    void testClassId() {
        String id = IdGenerator.classId(
            "mall-admin/src/main/java/com/mall/admin/service/ProductService.java",
            "ProductService"
        );
        assertTrue(id.startsWith("class//"));
        assertTrue(id.matches("class//[0-9a-f]{12}"));
    }

    @Test
    void testMethodId() {
        String classId = IdGenerator.classId(
            "mall-admin/src/main/java/com/mall/admin/service/ProductService.java",
            "ProductService"
        );
        String methodId = IdGenerator.methodId(classId, "queryProduct(Long)");
        assertTrue(methodId.startsWith("method//"));
        assertTrue(methodId.matches("method//[0-9a-f]{12}"));
    }

    @Test
    void testFieldId() {
        String classId = IdGenerator.classId("path/Foo.java", "Foo");
        String fieldId = IdGenerator.fieldId(classId, "name");
        assertTrue(fieldId.startsWith("field//"));
        assertTrue(fieldId.matches("field//[0-9a-f]{12}"));
    }

    @Test
    void testServiceId() {
        assertEquals("service://mall-admin", IdGenerator.serviceId("mall-admin"));
    }

    @Test
    void testApiEndpointId() {
        assertEquals("method//abc123def456#api", IdGenerator.apiEndpointId("method//abc123def456"));
    }

    @Test
    void testDeterministic() {
        // 同输入必须产出同 ID
        String id1 = IdGenerator.classId("a/B.java", "B");
        String id2 = IdGenerator.classId("a/B.java", "B");
        assertEquals(id1, id2, "Same input must produce same ID");
    }

    @Test
    void testDifferentInputsDifferentIds() {
        String id1 = IdGenerator.classId("a/B.java", "B");
        String id2 = IdGenerator.classId("a/C.java", "C");
        assertNotEquals(id1, id2, "Different inputs must produce different IDs");
    }

    @Test
    void testStableIdRawFormat() {
        // 验证底层 stableId 的哈希计算
        // Python: _stable_id("test", "hello", "world")
        // raw = "hello|world"
        // sha256("hello|world".encode()).hexdigest()[:12]
        String id = IdGenerator.stableId("test", "hello", "world");
        assertTrue(id.startsWith("test//"));
        assertEquals("test//" + id.substring(6), id);
        assertEquals(18, id.length()); // "test//" (6) + 12 hex = 18
    }

    @Test
    void testChineseCharacters() {
        // 确保中文字符正确处理 (UTF-8)
        String id = IdGenerator.stableId("class", "路径/文件.java", "中文类名");
        assertTrue(id.startsWith("class//"));
        assertTrue(id.matches("class//[0-9a-f]{12}"));
    }

    @Test
    void testEmptyParts() {
        // 空字符串部分
        String id = IdGenerator.stableId("class", "", "Foo");
        assertTrue(id.startsWith("class//"));
        // raw = "|Foo"
    }

    @Test
    void testSinglePart() {
        String id = IdGenerator.stableId("package", "com.example");
        assertTrue(id.startsWith("package//"));
    }

    // ===== Python 交叉验证 =====
    // 以下预期值由 Python _stable_id() 直接生成，Java 必须完全一致

    @Test
    void crossValidation_packageId() {
        assertEquals("package//980f7116f14e",
            IdGenerator.packageId("com.mall.admin.service"));
    }

    @Test
    void crossValidation_classId() {
        assertEquals("class//4cdf744b33dc",
            IdGenerator.classId(
                "mall-admin/src/main/java/com/mall/admin/service/ProductService.java",
                "ProductService"));
    }

    @Test
    void crossValidation_classId_simple() {
        assertEquals("class//6992c7606af6", IdGenerator.classId("a/B.java", "B"));
        assertEquals("class//e296a3cc2ba6", IdGenerator.classId("a/C.java", "C"));
    }

    @Test
    void crossValidation_testPrefix() {
        assertEquals("test//55a3db6314a8", IdGenerator.stableId("test", "hello", "world"));
    }

    @Test
    void crossValidation_emptyPath() {
        assertEquals("class//1081724d8d39", IdGenerator.classId("", "Foo"));
    }

    @Test
    void crossValidation_packageSimple() {
        assertEquals("package//95153502fc8b", IdGenerator.packageId("com.example"));
    }
}
