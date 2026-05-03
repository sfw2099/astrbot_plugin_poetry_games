# AstrBot 诗词游戏引擎

![Version](https://img.shields.io/badge/version-v3.2.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![License](https://img.shields.io/badge/license-AGPL--3.0-orange)

AstrBot 诗词游戏插件 - 飞花令 / 纵横飞花令 / 贪吃蛇诗词 / 诗词查询

## 快速开始

1. AstrBot 插件管理安装
2. 在群聊中发送 `/安装数据库` 自动下载并安装数据库
3. 发送 `/衔字飞花令` 或 `/纵横飞花令` 开始游戏

## 功能

### 游戏模式

| 指令 | 说明 |
|------|------|
| `/衔字飞花令` | 衔字规则接龙，匹配前两句汉字得分 |
| `/纵横飞花令 [宽] [高]` | 棋盘拼字，占领格子 |
| `/恢复游戏` | 查看存档列表，选择恢复 |
| `/结束游戏` | 结束当前游戏 |

### 诗词查询

| 指令 | 说明 |
|------|------|
| `/查询诗句 [内容]` | 精确 + 模糊双核搜索 |
| `/查询诗词 [标题] [作者]` | 标题搜索，支持作者过滤 |

### 管理

| 指令 | 说明 |
|------|------|
| `/安装数据库` | 多源探测 + 自动下载安装 |
| `/生成战报` | 查看当前游戏计分 |
| `/删除存档` | 删除旧的游戏存档 |
| `/飞花令帮助` | 查看完整帮助菜单 |

### 游戏内操作

`加入` / `退出` / `跳过` / `催更`

## 数据来源

数据库基于 [poetry-dataset](https://github.com/sfw2099/poetry-dataset) 构建，整合自：
- [chinese-poetry](https://github.com/chinese-poetry/chinese-poetry)
- [Werneror/Poetry](https://github.com/Werneror/Poetry)
- [poetic-mao](https://github.com/cdn0x12/poetic-mao)

共计 119 万首诗词，覆盖先秦至近现代。

## License

AGPL-3.0
