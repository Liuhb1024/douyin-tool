import hashlib
import unittest
from urllib.parse import urlencode

from app.douyin import (
    SEC_SALT,
    DouyinError,
    extract_aweme_id,
    extract_router_data,
    extract_url,
    filename_for,
    parse_share_html,
    post_from_item,
    safe_name,
    signed_params,
)

SHARE_HTML = """
<html><body><script>
window._ROUTER_DATA = {"loaderData":{"note_7624728488368732900/page":{"videoInfoRes":{"item_list":[{
  "aweme_id":"7624728488368732900",
  "desc":"爱情公寓｜第三季｜第257集",
  "aweme_type":4,
  "author":{"nickname":"示例作者"},
  "video":{
    "duration": 125000,
    "play_addr":{
      "uri":"v0200fg10000demo",
      "url_list":["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0200fg10000demo"]
    },
    "cover":{"url_list":["https://p3.douyinpic.com/cover.jpeg"]}
  }
}]}}}};
</script></body></html>
"""

PHOTO_HTML = """
<script>window._ROUTER_DATA = {"loaderData":{"page":{"videoInfoRes":{"item_list":[{
  "aweme_id":"100",
  "desc":"图集标题",
  "aweme_type":2,
  "author":{"nickname":"图集作者"},
  "images":[
    {"url_list":["https://p3.douyinpic.com/a.webp","https://p3.douyinpic.com/a.jpeg"]},
    {"url_list":["https://p3.douyinpic.com/b.jpeg"]}
  ],
  "video":{"play_addr":{"uri":"slideshow","url_list":["https://example.com/playwm/?video_id=slideshow"]}}
}]}}}};</script>
"""

FILTER_HTML = """
<script>window._ROUTER_DATA = {"loaderData":{"page":{"videoInfoRes":{
  "item_list":[],
  "filter_list":[{"detail_msg":"作品不见了"}]
}}}};</script>
"""


class ParseTests(unittest.TestCase):
    def test_extracts_url_from_share_text(self):
        text = "7.92 复制打开抖音 https://v.douyin.com/J4IQiholWbo/ 复制此链接，打开Dou音搜索"
        self.assertEqual(extract_url(text), "https://v.douyin.com/J4IQiholWbo")

    def test_modal_id_wins_over_profile_path(self):
        url = "https://www.douyin.com/user/MS4wLjABAAAA?modal_id=6953975910773099811"
        self.assertEqual(extract_aweme_id(url), "6953975910773099811")

    def test_parses_video_share_page(self):
        post = parse_share_html(SHARE_HTML, "7624728488368732900", "1080p")
        self.assertEqual(post.title, "爱情公寓｜第三季｜第257集")
        self.assertEqual(post.author, "示例作者")
        self.assertFalse(post.is_photo)
        self.assertEqual(post.duration_ms, 125000)
        self.assertIn("ratio=1080p", post.video_urls[0])
        self.assertIn("ratio=720p", post.video_urls[1])
        self.assertNotIn("playwm", "".join(post.video_urls))
        self.assertTrue(post.video_urls[-1].endswith("video_id=v0200fg10000demo"))
        self.assertTrue(extract_router_data(SHARE_HTML))

    def test_photo_post_uses_jpeg_not_slideshow(self):
        post = parse_share_html(PHOTO_HTML, "100")
        self.assertTrue(post.is_photo)
        self.assertEqual(post.image_urls, [
            "https://p3.douyinpic.com/a.jpeg",
            "https://p3.douyinpic.com/b.jpeg",
        ])
        self.assertEqual(post.video_urls, [])

    def test_filtered_post_explains_why(self):
        with self.assertRaises(DouyinError) as caught:
            parse_share_html(FILTER_HTML, "1")
        self.assertIn("作品不见了", str(caught.exception))

    def test_signature_uses_public_formula(self):
        params = signed_params({"aweme_id": "1", "aid": 6383}, "abc", "10")
        encoded = urlencode({key: params[key] for key in ("aweme_id", "aid", "uifid", "timestamp")})
        expected = hashlib.md5(f"abc_10_{SEC_SALT}_{encoded}".encode()).hexdigest()
        self.assertEqual(params["x-secsdk-web-signature"], expected)

    def test_prefers_h264_at_requested_height(self):
        item = {
            "aweme_id": "1",
            "desc": "标题",
            "aweme_type": 0,
            "author": {"nickname": "作者"},
            "video": {
                "duration": 1000,
                "play_addr": {"uri": "vid"},
                "bit_rate": [
                    {
                        "is_h265": 0,
                        "bit_rate": 1,
                        "play_addr": {
                            "width": 576,
                            "height": 1024,
                            "url_list": ["https://v.douyinvod.com/low.mp4"],
                        },
                    },
                    {
                        "is_h265": 1,
                        "bit_rate": 9,
                        "play_addr": {
                            "width": 1080,
                            "height": 1920,
                            "url_list": ["https://v.douyinvod.com/h265.mp4"],
                        },
                    },
                    {
                        "is_h265": 0,
                        "bit_rate": 8,
                        "play_addr": {
                            "width": 1080,
                            "height": 1920,
                            "url_list": ["https://v.douyinvod.com/h264.mp4"],
                        },
                    },
                ],
            },
        }
        high = post_from_item(item, "1", "1080p")
        low = post_from_item(item, "1", "540p")
        self.assertTrue(high.video_urls[0].endswith("h264.mp4"))
        self.assertTrue(low.video_urls[0].endswith("low.mp4"))

    def test_filename_strips_illegal_characters(self):
        post = parse_share_html(SHARE_HTML, "7624728488368732900")
        self.assertEqual(safe_name('a/b:c*'), "a b c")
        name = filename_for(post, "mp4")
        self.assertTrue(name.endswith("_7624728488368732900.mp4"))
        self.assertNotIn("/", name)


if __name__ == "__main__":
    unittest.main()
