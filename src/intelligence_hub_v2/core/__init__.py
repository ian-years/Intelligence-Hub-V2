"""core 层：配置、事件、清单、运行时环境。

依赖方向（docs/architecture.md 组件图）：`api/ → core/ → platforms/ → infra/ → storage/`。
core 只依赖 models 与 platforms.base，不依赖具体适配器实现。
"""
