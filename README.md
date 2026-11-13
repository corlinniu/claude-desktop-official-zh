# Claude Desktop 中文（官方词库）

让 Claude Desktop 的第三方推理模式（`Claude-3p`）也能选择中文界面。

Claude 桌面版的在线账号界面已原生支持中文，但第三方 API 模式加载的是随包的本地前端（`ion-dist.asar`），其中只有 11 种语言、没有中文。本工具直接使用 Anthropic 官方发布的中文词库，不维护社区翻译。

## 使用方法

双击 `install-mac.command`，或直接运行：

```bash
sudo /usr/bin/python3 scripts/patch_official_zh.py --user-home "$HOME" --lang zh-CN --launch
```

`--lang` 支持 `zh-CN`（简体）和 `zh-TW` / `zh-HK`（均使用官方繁体词库）。

安装后在左下角账号菜单 → `Language` 中选择「中文（简体）」或「中文（繁體）」。

还原官方 app：

```bash
sudo /usr/bin/python3 scripts/patch_official_zh.py --user-home "$HOME" --restore --launch
```

## 原理

1. 从 `https://claude.ai/i18n/zh-Hans.json` 下载 Anthropic 官方中文词库（静态资源，无需登录）。
2. 以本地 `i18n/en-US.json` 的 key 为准合并：官方有译文就用官方，没有就保留英文，不做额外翻译。
3. 写入 `ion-dist/i18n/zh-Hans.json` 和 `ion-dist/i18n/dynamic/zh-Hans.json`。
4. 把 `zh-Hans` / `zh-Hant` 加进前端的语言白名单，使其出现在 Language 菜单里。
5. 重打包 `ion-dist.asar`、同步 Electron 的 ASAR 完整性哈希、ad-hoc 重签，并备份原 app。

桌面壳层菜单直接使用 `app.asar` 里自带的官方 `zh-Hans` / `zh-Hant` 词库，无需额外资源。

## 说明

- 只支持 macOS，且需要 Claude `4.29+` 的 `ion-dist.asar` 布局。
- Claude 每次自动更新都会覆盖补丁，更新后重新运行一次即可。
- 语言包下载需要能访问 `claude.ai`。
- 卸载 / 回滚使用 `--restore`，会还原最近一次备份并把 locale 写回 `en-US`。

## 文件

- `install-mac.command`：双击入口。
- `scripts/patch_official_zh.py`：下载、合并、注册语言。
- `scripts/patcher_core.py`：ASAR 读写、完整性、签名、备份与 locale 等平台操作。

## 许可

见 [LICENSE](LICENSE)。
