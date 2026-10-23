# 抖音分享下载

粘贴一条抖音分享文案，把视频存到自己电脑上。图集会打成 zip。本机运行，不需要登录。

这是一个个人小工具，只处理你亲手粘贴的单条公开分享。

## 用法

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

打开 http://127.0.0.1:8000 ，把分享文案整段贴进去。短链、长链、带一堆文案的那种复制文本都可以。

不想开页面时：

```bash
python run.py "7.43 复制打开抖音…… https://v.douyin.com/xxxxxx/"
```

文件写到 `downloads/`。

Python 3.11 及以上。依赖只有 FastAPI、Uvicorn 和 requests。

## 它在做什么

分享文案里的 `v.douyin.com/xxxx` 不是视频地址，只是一次跳转。跳完才能看到作品编号 `aweme_id`。长链 `www.douyin.com/video/数字` 里这个编号已经在路径上。

编号还不能直接播。抖音的详情接口会检查两个东西：`uifid`，以及用它算出来的 `x-secsdk-web-signature`。缺任何一个都是 403。评论接口才要的 `a_bogus`，单条视频用不上。

分享页现在也不再把视频数据嵌在 HTML 里。`window._ROUTER_DATA` 只剩页面配置，所以不能再靠抠网页。

工具的做法：

1. 从文案里抽出抖音链接，跟随跳转，读出 `aweme_id`。主页、直播、音乐页在这里就停。
2. 向 `/ttwid/register` 要一个匿名 `ttwid`。`passport_csrf_token` 本地随便生成一段即可。
3. 打开首页。有 `UIFID` Cookie 就用它，否则用响应头里的 `x-tt-trace-host` 当作 `uifid`。
4. 对查询参数做 MD5：`uifid + 时间戳 + 页面脚本里的盐 + query`。盐不包含签名自己。
5. 请求 `aweme/detail`。查询里带 `aid=6383`，否则经常只有 720p。
6. 在 `video.bit_rate` 里选档：优先 H.264，短边不超过所选清晰度，同档取得码率更高的。地址里的 `playwm` 换成 `play`。
7. 带着 `Referer: https://www.douyin.com/` 去 CDN 拉文件。不带这个头，`douyinvod.com` 会拒绝。

浏览器不能自己完成第 7 步：跨域，而且页面改不了 `Referer`。所以下载由本机后端转发，前端只负责粘贴和保存。

```mermaid
flowchart LR
  text[分享文案] --> id[抽出链接并得到作品编号]
  id --> ticket[匿名 ttwid 和 uifid]
  ticket --> sign[给详情接口签名]
  sign --> meta[标题、封面、档位地址]
  meta --> cdn[带 Referer 拉取 CDN]
  cdn --> save[浏览器或 downloads 目录]
```

解析结果在内存里放大约 3 分钟。CDN 地址会过期，点下载时会重新要一次详情。文件头里要能看到 MP4 的 `ftyp`，否则不会把一段 JSON 或验证页存成视频。

图集看 `aweme_type`（2 或 68）或者有没有 `images`。每张图取 `url_list` 的最后一条，通常是 JPEG，打进一个 zip。

转发只允许抖音自己的站点和它的 CDN。这个接口不能被拿去请求别的地址。

## 目录

```text
.
├── app
│   ├── douyin.py          # 解析、签名、选档、下载
│   ├── main.py            # FastAPI：页面、解析、封面、下载
│   └── static/index.html  # 粘贴用的单页
├── tests/test_douyin.py   # 链接提取、签名公式、档位选择
├── run.py                 # 无参数启动页面，有参数则直接下载
├── requirements.txt
└── .gitignore
```

`.venv/`、`downloads/` 和 `__pycache__/` 不进仓库。

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/parse` | `{ "text": "分享文案", "quality": "1080p" }`，返回标题、作者、时长 |
| `GET` | `/api/download/{aweme_id}?quality=1080p` | 视频流，或图集 zip |
| `GET` | `/api/cover/{aweme_id}` | 封面。浏览器直接引用抖音图床会被拒绝 |
| `GET` | `/api/health` | 进程是否活着 |

`quality` 可以是 `1080p`、`720p`、`540p`。作品没有正好这一档时，取不超过它的最高一档。

## 什么时候会失效

抖音改的是校验，不是「这个接口还在不在」。下面三处一变，下载就会停：

- 签名用的盐换了
- `uifid` 不再放在 `x-tt-trace-host`
- `aid=6383` 不再对应高清档

失败时接口多半仍是 403，正文是 `Uifid Not Found`、`Signature Not Found` 或 `Sign Invalid`。改 `app/douyin.py` 里的 `_ensure_web` 和 `signed_params`，页面不用动。

作品已删除、设为私密，或者链接其实是主页，会直接告诉你原因，不会存一个空文件。
