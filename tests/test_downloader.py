from threadstractormf.downloader import is_valid_media_url


def test_valid_cdn_with_qs():
    assert is_valid_media_url("https://scontent.cdninstagram.com/v/t51.123/x.jpg?_nc_cat=1&_nc_sid=123")
    assert is_valid_media_url("https://cdninstagram.com/image/123.jpg")


def test_invalid_not_cdn():
    assert not is_valid_media_url("https://example.com/x.jpg")
    assert not is_valid_media_url("http://scontent.cdninstagram.com/x.jpg")  # no https
