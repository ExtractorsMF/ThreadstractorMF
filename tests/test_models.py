from threadstractormf.models import Media, extract_post_id, sanitize_filename


def test_extract_post_id():
    assert extract_post_id("https://www.threads.com/@user/post/C123abc") == "C123abc"
    assert extract_post_id("https://www.threads.net/@user/post/DKxyz/media") == "DKxyz"
    assert extract_post_id("https://example.com/") is None


def test_media_filename_single():
    m = Media(
        id="C123", post_id="C123", index=1, type="image",
        url="https://scontent.cdninstagram.com/x.jpg", ext="jpg",
    )
    assert m.filename() == "C123.jpg"


def test_media_filename_carousel():
    m = Media(
        id="C123_2", post_id="C123", index=2, type="image",
        url="https://scontent.cdninstagram.com/x.jpg", ext="jpg",
    )
    assert m.filename() == "C123_2.jpg"


def test_sanitize():
    assert sanitize_filename("../a/b") == "a_b"
    assert sanitize_filename("..a") == "a"
    assert len(sanitize_filename("x" * 200)) == 100
