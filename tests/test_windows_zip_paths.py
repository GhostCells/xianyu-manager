import hashlib
import zipfile
import pytest
from xianyu_manager.delivery_package import check_zip


@pytest.mark.parametrize('name', [r'商品\说明.txt', r'商品\子目录/readme.txt'])
def test_windows_separator_accepted_without_modification(tmp_path,name):
    p=tmp_path/'delivery.zip'
    with zipfile.ZipFile(p,'w') as z:z.writestr(name,'synthetic documentation')
    before=hashlib.sha256(p.read_bytes()).hexdigest()
    check_zip(p)
    assert hashlib.sha256(p.read_bytes()).hexdigest()==before
    assert list(tmp_path.iterdir())==[p]


@pytest.mark.parametrize('name',[r'..\evil.txt',r'a\..\evil.txt',r'a/..\evil.txt',r'\root.txt',r'\\server\share\file',r'C:\file',r'C:file','../evil.txt','/root.txt'])
def test_unsafe_paths_still_rejected(tmp_path,name):
    p=tmp_path/'delivery.zip'
    with zipfile.ZipFile(p,'w') as z:z.writestr(name,'synthetic')
    with pytest.raises(ValueError):check_zip(p)


def test_windows_symlink_still_rejected(tmp_path):
    p=tmp_path/'delivery.zip';entry=zipfile.ZipInfo(r'folder\link')
    entry.create_system=3;entry.external_attr=0o120777<<16
    with zipfile.ZipFile(p,'w') as z:z.writestr(entry,'target')
    with pytest.raises(ValueError):check_zip(p)
