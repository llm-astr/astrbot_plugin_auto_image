# astrbot_plugin_auto_image

AstrBot 插件：**LLM 自主生图 / 改图**。bot 在对话中根据上下文自主决定生成或修改图片，无需记忆指令；生图参数（模型 / 画面比例 / 清晰度）支持**关键词模糊匹配**。API 后端为 Grsai，与 [astrbot_plugin_grsai_image](https://github.com/llm-astr/astrbot_plugin_grsai_image) 同一接口。

## 功能

- **LLM 自主调用**：注册 `auto_text_to_image`（文生图）与 `auto_image_edit`（图生图/改图）两个 LLM 函数工具，对话中说到「画一张…」「把这张图改成…」时 bot 自动调用并发送图片
- **关键词模糊匹配**：模型、比例、清晰度均可中文/英文模糊指定（见下表），匹配不到自动回退插件默认配置
- **改图自动取图**：自动使用当前消息图片、回复引用的图片、或会话中最近 10 分钟内发送的图片作为原图
- **指令兜底**：保留 `/生图`、`/改图`、`/生图模型` 指令用于精确控制
- 平台图链（QQ 等多媒体链接）统一转 base64 data URL 再提交，避免 API 拉取失败

## 使用示例

### LLM 自主模式（无需指令）

```
用户：给我画一只在月球上喝茶的橘猫，横版 4K
bot ：🎨 已提交生图任务（nano-banana-fast / 16:9 / 4K），生成中请稍候……
      [图片]

用户：（发一张自拍）帮我改成吉卜力风格
bot ：🎨 已提交生图任务（nano-banana-fast / auto / 1K，参考图 1 张），生成中请稍候……
      [图片]
```

### 指令模式

```
/生图 一只在月球上喝茶的橘猫 横版 4K pro
/生图 赛博朋克城市夜景 -m nano-banana-pro -ar 21:9 -s 4K
（回复一张图片）/生图 把背景换成海边
/改图 改成水彩画风格 高清
/生图模型
```

### 模糊关键词表

| 参数 | 可用关键词（节选） |
| --- | --- |
| 模型 | fast / 快速、香蕉 / banana、pro / 专业、香蕉2 / 二代 / nb2、gpt / openai、vip / 会员 … |
| 比例 | 横版/横屏/壁纸(16:9)、竖版/竖屏/手机壁纸(9:16)、方图/头像(1:1)、电影/超宽(21:9)、小红书(3:4)、或直接写 16:9 |
| 清晰度 | 标清(1K)、高清(2K)、超清/超高清(4K)、或直接写 1K/2K/4K |

> 注：仅 nano-banana pro / 2 系列支持 1K-4K 清晰度参数，其他模型自动忽略。

## 配置

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| api_key | Grsai API Key（必填） | 空 |
| api_node | API 节点：cn / global | cn |
| model | 默认生图模型 | nano-banana-fast |
| aspect_ratio | 默认画面比例 | auto |
| image_size | 默认清晰度 | 1K |
| show_progress_msg | 是否发送「生成中」提示 | true |
| poll_timeout | 等待生成结果最长时间（秒） | 300 |
| poll_interval | 结果轮询间隔（秒） | 4 |

## 安装

1. 将本插件放入 AstrBot `data/plugins/` 目录，或在管理面板通过仓库地址安装
2. 安装依赖：`pip install -r requirements.txt`
3. 在插件配置中填写 Grsai API Key
4. **LLM 自主生图需要**：使用的 LLM 供应商支持函数调用（function calling），且 AstrBot 中未禁用工具调用

## 说明

- 参考图限制：最多 8 张，单张小于 10MB
- 生成结果图片 URL 有效期约 2 小时，插件会先下载到本地再发送，发送后即删除临时文件
- 会话图片缓存仅保存在内存中（10 分钟过期），重启即清空

## License

MIT
