"""设计模式 / 架构模式词表（用于约束 LLM 输出）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatternItem:
    pattern_type: str  # design | architecture
    name: str
    hint: str


# GoF 23 design patterns
DESIGN_PATTERNS: list[PatternItem] = [
    PatternItem("design", "Singleton", "确保一个类只有一个实例，并提供全局访问点。"),
    PatternItem("design", "Factory Method", "定义创建对象的接口，让子类决定实例化哪一类。"),
    PatternItem("design", "Abstract Factory", "提供创建一组相关/依赖对象的接口，不指定具体类。"),
    PatternItem("design", "Builder", "将复杂对象的构建与表示分离，同样的构建过程可创建不同表示。"),
    PatternItem("design", "Prototype", "用原型实例指定创建对象的类型，通过复制创建新对象。"),
    PatternItem("design", "Adapter", "将一个类的接口转换成另一个接口，使原本不兼容的类能协同工作。"),
    PatternItem("design", "Decorator", "动态地给对象添加新职责（比继承更灵活）。"),
    PatternItem("design", "Facade", "为子系统提供统一的简化接口。"),
    PatternItem("design", "Bridge", "将抽象与实现分离，使它们都可以独立变化。"),
    PatternItem("design", "Composite", "将对象组合成树形结构以表示“整体-部分”层次结构。"),
    PatternItem("design", "Flyweight", "运用共享来有效支持大量细粒度对象。"),
    PatternItem("design", "Proxy", "为其他对象提供一种代理以控制访问。"),
    PatternItem("design", "Chain of Responsibility", "将请求沿链传递，直到有对象处理它为止。"),
    PatternItem("design", "Command", "将请求封装为对象，从而参数化不同请求、支持队列/日志/撤销等。"),
    PatternItem("design", "Mediator", "用中介对象封装对象间的交互，减少类之间的耦合。"),
    PatternItem("design", "Iterator", "提供一种方法顺序访问聚合对象，而不暴露其内部表示。"),
    PatternItem("design", "Template Method", "定义算法骨架，延迟某些步骤到子类实现。"),
    PatternItem("design", "Observer", "定义对象间的一种一对多依赖，使依赖者能自动通知并更新。"),
    PatternItem("design", "State", "允许对象在内部状态改变时改变行为，表现为对象像改变了类。"),
    PatternItem("design", "Strategy", "定义一组算法，把它们封装起来，并且使它们可以相互替换。"),
    PatternItem("design", "Visitor", "表示作用于某对象结构中的各元素的操作。使你可以在不改变各元素类的前提下定义新操作。"),
    PatternItem("design", "Memento", "在不破坏封装的前提下，捕获并外部化对象的内部状态，以便之后恢复。"),
    PatternItem("design", "Interpreter", "给定一个语言，定义它的文法表示，并为该文法定义解释器。"),
    PatternItem("design", "Command (Redo/Undo)", "命令模式在工程里常被用于撤销/重做语义（作为工程信号的补充提示）。"),
    PatternItem("design", "Proxy (Lazy/Access)", "代理模式在工程里经常以延迟加载/访问控制体现（作为工程信号的补充提示）。"),
]

# 说明：上面补充了两个“工程常见信号”的名字，严格来说会超过 23。
# 为避免 LLM 输出“近似但不在 GoF 23”的名称，这里我们在 runner 里会再过滤/映射到最终允许集合。

# Common architecture patterns (approximation list)
ARCHITECTURE_PATTERNS: list[PatternItem] = [
    PatternItem("architecture", "Layered Architecture", "典型分层（Controller/Service/Repository 等），依赖方向通常自上而下。"),
    PatternItem("architecture", "MVC (Model-View-Controller)", "控制层与业务/数据分离，通过路由/控制器承载请求。"),
    PatternItem("architecture", "Hexagonal Architecture", "端口与适配器（Ports & Adapters），核心领域与外部技术隔离。"),
    PatternItem("architecture", "Clean Architecture", "用用例/实体/接口隔离依赖，保持规则面向领域。"),
    PatternItem("architecture", "Onion Architecture", "多层同心依赖，外层依赖内层，内层独立性更强。"),
    PatternItem("architecture", "Microservices Architecture", "按服务边界拆分模块，服务间调用通过依赖/调用链体现。"),
    PatternItem("architecture", "Event-Driven Architecture", "通过事件/订阅/消息机制解耦（Listener/Subscriber/Publish/Consume 等信号）。"),
    PatternItem("architecture", "CQRS", "命令侧与查询侧拆分（write/read 分离）并由不同模型与处理链体现。"),
    PatternItem("architecture", "DDD - Layered", "领域驱动设计的分层表达（Domain 应用在核心层，基础设施与表现层外置）。"),
    PatternItem("architecture", "Plugin/Extension Architecture", "以扩展点/注册表/策略集合实现可插拔能力（常见为 extension points）。"),
]


def allowed_pattern_names() -> tuple[list[str], list[str]]:
    """返回 runner 用的最终允许集合（design_names, architecture_names）。"""
    design_names = [
        # 仅保留 GoF 23 的官方名称集合（包含空格/连字符等写法保持一致）
        "Singleton",
        "Factory Method",
        "Abstract Factory",
        "Builder",
        "Prototype",
        "Adapter",
        "Decorator",
        "Facade",
        "Bridge",
        "Composite",
        "Flyweight",
        "Proxy",
        "Chain of Responsibility",
        "Command",
        "Mediator",
        "Iterator",
        "Template Method",
        "Observer",
        "State",
        "Strategy",
        "Visitor",
        "Memento",
        "Interpreter",
    ]
    arch_names = [p.name for p in ARCHITECTURE_PATTERNS]
    return design_names, arch_names


def format_allowed_patterns_for_prompt() -> str:
    design_names, arch_names = allowed_pattern_names()
    return (
        "Design patterns (GoF 23, pattern_name 必须从下面列表中严格选择):\n"
        + "\n".join([f"- {n}" for n in design_names])
        + "\n\nArchitecture patterns (common, pattern_name 必须从下面列表中严格选择):\n"
        + "\n".join([f"- {n}" for n in arch_names])
    )

