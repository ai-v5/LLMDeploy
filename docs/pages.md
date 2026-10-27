# GitHub Pages 与搜索发现

网站地址：<https://ai-v5.github.io/LLMDeploy/>。部署说明以 Markdown 为内容源；首页使用独立展示模板，只介绍项目和核心部署入口，不另存一套操作说明。

## 构建与发布

`site/config.json` 明确列出页面。`scripts/build_site.py` 只读取该清单中的公开 Markdown、仅用于 `index.html` 的固定 `site/home.html` 模板、固定 CSS，以及显式声明的 Google 验证文件。首页沿用文档的浅色样式，只保留项目介绍和核心部署入口，不使用文档侧栏；部署文档保留原有阅读布局。不引入脚本或第三方资源，不复制整个仓库，不读取 `local/`、私有机器配置、原始日志或用户会话。站内文档链接转换为网站 URL，配方与源码链接指回 GitHub。

本地预览采用独立环境，输出目录必须不存在：

```sh
python -m venv .venv
.venv/bin/python -m pip install -r site/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/build_site.py --output local/site-preview
.venv/bin/python -m http.server 8080 --directory local/site-preview
```

Windows 使用 `.venv/Scripts/python.exe`。本地 HTTP 预览保留正式 canonical 与文档 URL，CSS 使用相对路径；可直接访问相应 HTML 检查页面结构。

GitHub Settings → Pages → Build and deployment 选择 **GitHub Actions**。`pages.yml` 对 PR 运行单测和静态构建；只有 `main` 的 push 或手动运行才能上传构建目录并部署。发布权限仅授予部署 job。不上传仓库根目录。网站包含语言标记、独立 title/description、canonical、源码链接和 `sitemap.xml`。

新增 A2、A3 时先填写[部署模板](deployments/TEMPLATE.md)，按实际型号/引擎登记 `recipes/catalog.json`，再把文档加入站点清单；未验收项保持待验证标识。

## Google Search Console

本网站已采用 **HTML 文件上传**方式：验证文件 `googlef1de4c0e8dff2f1b.html` 保存在 `site/verification/`，由 `site/config.json` 的 `google_verification_files` 明确列出。构建器检查文件名和 Google 验证标记后按字节原样复制到网站根目录，不套用页面模板，也不加入 sitemap。验证地址为 <https://ai-v5.github.io/LLMDeploy/googlef1de4c0e8dff2f1b.html>。

站点所有者在 Search Console 的对应 URL 前缀资源中选择 HTML 文件方式并点击“验证”。**成功后仍须保留文件及发布清单项**，后续每次发布都会持续携带该文件；文件可访问不代表已经替账号完成验证。此文件是用于公开发布的所有权验证标记，不包含 Google 登录凭据。也可按下述说明使用 HTML 标记作为额外验证方式。

GitHub Pages 有利于提供独立可抓取的文档，但**不能强迫 Google 收录，也不保证排名或 GitHub 仓库收录加速**。仓库链接便于读者与爬虫发现源码，不是排名承诺。参见 [Google 抓取与重新索引说明](https://developers.google.com/search/docs/crawling-indexing/ask-google-to-recrawl)。

由站点所有者登录 [Search Console](https://search.google.com/search-console)，添加 URL 前缀资源 `https://ai-v5.github.io/LLMDeploy/`，使用该账号生成的 HTML 标记验证方式。把其 `content` 值填入 `site/config.json` 的 `google_site_verification`；不要填写整个 HTML 标签，不要放 Google 登录凭据。该公开验证标记会进入所有页面的 head；没有真实值时不生成占位标签。

合并发布后，在 Google 完成所有权验证，提交 `https://ai-v5.github.io/LLMDeploy/sitemap.xml`，并用 URL 检查工具请求首页与重要部署页面编入索引。无需反复提交同一 URL。此操作由 Google 账号所有者完成，GitHub 发布成功不等于 Search Console 已验证或 Google 已收录。参见 [Google 所有权验证文档](https://support.google.com/webmasters/answer/9008080)。

本仓生成的 `/LLMDeploy/robots.txt` 只是项目路径下的说明文件；爬虫实际读取的是来源根路径 `https://ai-v5.github.io/robots.txt`。不能用项目子目录文件控制整个来源；本任务不修改组织根站点。直接提交 sitemap，不依赖子目录 robots 的发现。
