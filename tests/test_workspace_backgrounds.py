from datetime import date
from pathlib import Path
import json
import pytest
from PIL import Image
from rem_card.app.workspace_backgrounds import BackgroundRepository, read_catalog, validate_image, builtin_paths

@pytest.fixture
def pair(tmp_path):
    paths=[]
    for mode,color in [('light','#e8e0d7'),('dark','#19232f')]:
        path=tmp_path/(mode+'.png')
        Image.new('RGB',(3840,2160),color).save(path)
        paths.append(path)
    return paths

@pytest.fixture
def repository(tmp_path):
    return BackgroundRepository(tmp_path/'shared',tmp_path/'cache')

def publish(repository,pair,**kwargs):
    return repository.update('publish',light=pair[0],dark=pair[1],name='Тестовый фон',**kwargs)

def test_pair_publishes_without_sqlite_and_downloads_once(repository,pair,monkeypatch):
    catalog,status=publish(repository,pair)
    entry=catalog['entries'][0]
    assert status[entry['id']]=='На сервере'
    assert repository.active()[0]['id']==entry['id']
    assert set(repository.active()[1])=={'light','dark'}
    assert not list(repository.shared.parents[2].rglob('*.db'))
    files=[Path(x) for x in repository.pair(entry).values()]
    times=[x.stat().st_mtime_ns for x in files]
    repository.sync()
    assert times==[x.stat().st_mtime_ns for x in files]

def test_missing_server_preserves_local_pair_new_pc_falls_back(repository,pair,tmp_path):
    catalog,_=publish(repository,pair)
    entry=catalog['entries'][0]
    for mode in ('light','dark'):
        (repository.shared/entry['id']/entry[mode]['file']).unlink()
    _,status=repository.sync()
    assert status[entry['id']]=='Только локально'
    assert repository.active()[0]['id']==entry['id']
    fresh=BackgroundRepository(repository.shared.parents[2],tmp_path/'new-cache')
    fresh.sync()
    assert fresh.active()[1]==builtin_paths()

def test_broken_second_file_never_switches_half_pair(repository,pair):
    catalog,_=publish(repository,pair)
    previous=catalog['entries'][0]
    # Publish a new pair from another workstation, then damage its dark file.
    other=BackgroundRepository(repository.shared.parents[2],repository.local/'other')
    catalog,_=publish(other,pair)
    new=catalog['entries'][-1]
    (repository.shared/new['id']/new['dark']['file']).write_bytes(b'broken')
    repository.sync()
    assert repository.active()[0]['id']==previous['id']

def test_holiday_expiry_and_catalog_removal(repository,pair):
    publish(repository,pair)
    catalog,_=publish(repository,pair,start='2026-12-30',end='2027-01-02')
    entry=catalog['entries'][-1]
    assert repository.active(today=date(2026,12,31))[0]['id']==entry['id']
    assert repository.active(today=date(2027,1,3))[0]['id']==catalog['default']
    with pytest.raises(ValueError,match='пересекается'):
        publish(repository,pair,start='2027-01-01',end='2027-01-05')
    repository.update('remove',entry_id=catalog['default'])
    assert repository.active(today=date(2027,1,3))[1]==builtin_paths()
    assert (repository.shared/entry['id']).is_dir()

def test_validation_and_lock_preserve_catalog(repository,pair):
    catalog,_=publish(repository,pair)
    with pytest.raises((ValueError, OSError)):
        publish(repository,[pair[0],pair[0].parent/'missing.png'])
    assert read_catalog(repository.shared/'catalog.json')==catalog
    (repository.shared/'.publication.lock').touch()
    with pytest.raises(ValueError,match='Другой компьютер'):
        repository.update('default',entry_id='builtin')
    assert read_catalog(repository.shared/'catalog.json')==catalog

@pytest.mark.parametrize('size,mode', [((1672,941),'RGB'),((3840,2160),'RGBA')])
def test_import_rejects_low_resolution_and_alpha(tmp_path,size,mode):
    p=tmp_path/'bad.png'
    Image.new(mode,size).save(p)
    with pytest.raises(ValueError):
        validate_image(p)

def test_catalog_rejects_path_traversal(repository,pair):
    catalog,_=publish(repository,pair)
    catalog['entries'][0]['id']='../outside'
    (repository.shared/'catalog.json').write_text(json.dumps(catalog),encoding='utf-8')
    with pytest.raises(ValueError):
        repository.sync()


def test_swapped_themes_are_rejected_before_publication(repository,pair):
    with pytest.raises(ValueError,match='перепутаны'):
        publish(repository,list(reversed(pair)))
    assert not (repository.shared/'catalog.json').exists()
