from rem_card.app import local_administrator as access


def test_grant_is_local_installation_and_version_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    identity = {'host': 'pc1', 'installation': 'C:/RemCard/RemCard.exe', 'version': '1.0'}
    monkeypatch.setattr(access, '_identity', lambda: dict(identity))
    assert not access.is_local_administrator()
    access.set_local_administrator(True)
    assert access.is_local_administrator()
    for key in identity:
        saved = identity[key]
        identity[key] += '-different'
        assert not access.is_local_administrator()
        identity[key] = saved
    assert access.is_local_administrator()
    access.set_local_administrator(False)
    assert not access.is_local_administrator()


def test_invalid_grant_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    path = access._path()
    path.parent.mkdir(parents=True)
    path.write_text('{broken', encoding='utf-8')
    assert not access.is_local_administrator()
