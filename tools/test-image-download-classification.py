#!/usr/bin/env python3
"""Execute the actual CTextureCacheJob::IsInterruptibleDownload on image paths.

IsInterruptibleDownload and its owner helpers are extracted from
TextureCacheJob.cpp and run against a minimal CURL fixture (protocol, owner,
percent-decoded image:// host, options). Covers: plain HTTP(S), FTP(S) and
WebDAV reads download; other protocols (SMB, NFS, SFTP and UPnP, whose backends
ignore the read interrupt) and local files do not; inside image://, a transform,
flipped, a PVR owner or an unknown owner still downloads, while a generated
image (video, music, picture folder thumbnails) does not.
The fixture is not Kodi's CURL parser.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('class_extract', ROOT / 'tools/test-aml-texture-upload-context.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
SOURCE = 'xbmc/TextureCacheJob.cpp'

PRELUDE = r'''
#include <cstdio>
#include <stdexcept>
#include <string>
static unsigned checks=0;
static void Check(bool ok,const char*text){++checks;if(!ok)throw std::runtime_error(text);}
struct StringUtils {static bool StartsWith(const std::string&s,const std::string&p){return s.rfind(p,0)==0;}};
// protocol://[user@]host[/path][?options]; an image:// host is percent-decoded.
class CURL {
 std::string m_protocol,m_user,m_host,m_options;
 static std::string Decode(const std::string&s){std::string r;for(size_t i=0;i<s.size();++i){if(s[i]=='%'&&i+2<s.size()){r+=char(std::stoi(s.substr(i+1,2),nullptr,16));i+=2;}else r+=s[i];}return r;}
public:
 explicit CURL(const std::string&url){
  const auto sep=url.find("://");if(sep==std::string::npos)return;
  m_protocol=url.substr(0,sep);std::string rest=url.substr(sep+3);
  const auto q=rest.find('?');if(q!=std::string::npos){m_options=rest.substr(q+1);rest=rest.substr(0,q);}
  const auto slash=rest.find('/');std::string authority=rest.substr(0,slash);
  const auto at=authority.find('@');if(at!=std::string::npos){m_user=authority.substr(0,at);authority=authority.substr(at+1);}
  m_host=m_protocol=="image"?Decode(authority):authority;}
 const std::string& GetUserName()const{return m_user;}
 const std::string& GetHostName()const{return m_host;}
 bool IsProtocol(const std::string&p)const{return m_protocol==p;}
 bool HasOption(const std::string&o)const{return ("&"+m_options+"&").find("&"+o)!=std::string::npos;}
};
struct CTextureCacheJob {static bool IsInterruptibleDownload(const std::string& image);};
// path substitution: http://mapped/ is read from smb://nas/
struct URIUtils {static std::string SubstitutePath(const std::string&p,bool=false){return p.rfind("http://mapped/",0)==0?"smb://nas/"+p.substr(14):p;}};
static std::string vfsProtocol;
namespace XFILE {struct CFileFactory {static bool IsVFSAddonProtocol(const CURL&u){return !vfsProtocol.empty()&&u.IsProtocol(vfsProtocol);}};}
'''

TESTS = r'''
static void Expect(const char*path,bool expected,const char*text){Check(CTextureCacheJob::IsInterruptibleDownload(path)==expected,text);}
int main(){try{
 for(auto p:{"http://x/a.jpg","https://x/a.jpg","ftp://x/a.jpg","ftps://x/a.jpg","dav://x/a.jpg","davs://x/a.jpg"})Expect(p,true,"a plain HTTP(S), FTP(S) or WebDAV read downloads");
 for(auto p:{"smb://x/a.jpg","nfs://x/a.jpg","sftp://x/a.jpg","upnp://x/1","/storage/a.jpg","special://home/a.jpg"})Expect(p,false,"other protocols and local files do not download");
 Expect("image://http%3a%2f%2fx%2fa.jpg/",true,"a wrapped HTTP image downloads");
 Expect("image://http%3a%2f%2fx%2fa.jpg/transform?size=thumb",true,"a transformed HTTP image downloads");
 Expect("image://pvrchannel_tv@http%3a%2f%2fx%2flogo.png/",true,"a PVR-owned HTTP image downloads");
 Expect("image://epgtag_7@http%3a%2f%2fx%2fe.jpg/",true,"a PVR-owned HTTP image downloads");
 Expect("image://someone@http%3a%2f%2fx%2fa.jpg/",true,"an unknown owner does not hide a plain read");
 for(auto p:{"image://video@http%3a%2f%2fx%2fm.mkv/","image://music@http%3a%2f%2fx%2fs.flac/","image://picturefolder@http%3a%2f%2fx%2fdir%2f/","image://video_5@http%3a%2f%2fx%2fm.mkv/"})
  Expect(p,false,"a generated image does not download");
 Expect("image://video@http%3a%2f%2fx%2fm.jpg/?flipped",true,"flipped overrides the owner, as DecodeImageURL does");
 Expect("image://%2fstorage%2fa.jpg/",false,"a wrapped local image does not download");
 Expect("http://mapped/a.jpg",false,"an image substituted to another protocol does not download");
 Expect("image://http%3a%2f%2fmapped%2fa.jpg/",false,"an image substituted to another protocol does not download");
 vfsProtocol="davs";Expect("davs://x/a.jpg",false,"a protocol that a VFS add-on serves does not download");
 Expect("https://x/a.jpg",true,"other curl protocols still download");vfsProtocol.clear();
 std::printf("PASS %u checks; actual IsInterruptibleDownload, CURL fixture\n",checks);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def anon(text, name):
    m = re.search(r'^bool ' + name + r'\(.*?\n\}\n', text, re.S | re.M)
    assert m, name
    return m.group(0)


def source(text, mutation=None):
    body = '\n'.join([anon(text, 'IsPVROwnedImage'), anon(text, 'IsGeneratorOwner'),
                      EXTRACT.function(text, 'bool CTextureCacheJob::IsInterruptibleDownload(')])
    if mutation:
        old, new = mutation
        assert body.count(old) == 1, old
        body = body.replace(old, new, 1)
    return PRELUDE + body + TESTS


MUTATIONS = [
    ('owner-ignored', ('    if (!url.HasOption("flipped") && IsGeneratorOwner(url.GetUserName()))\n      return false;\n', ''),
     'a generated image does not download'),
    ('flipped-ignored', ('!url.HasOption("flipped") && IsGeneratorOwner', 'IsGeneratorOwner'),
     'flipped overrides the owner, as DecodeImageURL does'),
    ('pvr-generated', ('  return kept && !IsPVROwnedImage(owner);', '  return kept;'), 'a PVR-owned HTTP image downloads'),
    ('any-protocol', ('  const bool curl = url.IsProtocol("http") ||', '  const bool curl = true ||'), 'other protocols and local files do not download'),
    ('wrapper-unread', ('    source = url.GetHostName();\n', ''), 'a wrapped HTTP image downloads'),
    ('no-substitution', ('  const CURL url(URIUtils::SubstitutePath(source));', '  const CURL url(source);'),
     'an image substituted to another protocol does not download'),
    ('vfs-ignored', ('  return curl && !XFILE::CFileFactory::IsVFSAddonProtocol(url);', '  return curl;'),
     'a protocol that a VFS add-on serves does not download'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', '--out-dir', dest='out', type=Path)
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix='image-download-classification-'))
    out.mkdir(parents=True, exist_ok=True)
    text = (ROOT / SOURCE).read_text()
    records = []
    for name, mutation, witness in [('current', None, None), *MUTATIONS]:
        cpp = out / (name + '.cpp')
        cpp.write_text(source(text, mutation))
        exe = out / name
        cmd = ['g++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', str(cpp), '-o', str(exe)]
        c = subprocess.run(cmd, capture_output=True, text=True)
        assert c.returncode == 0, c.stderr
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        accepted = (r.returncode == 0 and not r.stderr) if witness is None else (
            r.returncode == 1 and r.stderr == 'FAIL ' + witness + '\n')
        records.append({'name': name, 'exit': r.returncode, 'witness': witness, 'accepted': accepted})
        (out / 'receipt.json').write_text(json.dumps({'results': records}, indent=2) + '\n')
        assert accepted, (name, r.stdout, r.stderr)
        print(r.stdout, end='') if witness is None else print('PASS semantic control ' + name + ': ' + witness)


if __name__ == '__main__':
    main()
