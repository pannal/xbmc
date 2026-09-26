#!/usr/bin/env python3
"""Execute application player close/replacement methods with recording players.

Checks pending close preserves the original target and destruction occurs outside
its application lock. Actual media threads/GUI callbacks are not simulated here.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
PRELUDE = r'''
#include <cassert>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
struct CCriticalSection {
  bool held=false;
  void lock(){assert(!held);held=true;}
  void unlock(){assert(held);held=false;}
};
struct CFileItem {bool IsDiscImage()const{return false;}bool IsDVDFile()const{return false;}};
struct CPlayerOptions {};
struct IPlayerCallback {};
struct IPlayer {
  std::string m_name="old",m_type="video";
  bool playing=false,canClose=false;int closes=0,opens=0;
  CCriticalSection* applicationLock=nullptr;int* destroyed=nullptr;
  ~IPlayer(){assert(!applicationLock||!applicationLock->held);if(destroyed)++*destroyed;}
  bool IsPlaying(){return playing;}
  bool CloseFile(bool=false){++closes;return canClose;}
  bool OpenFile(const CFileItem&,const CPlayerOptions&){++opens;return true;}
};
struct CPlayerCoreFactory {
  std::string GetDefaultPlayer(const CFileItem&)const{return "new";}
};
struct Timer {int expires=0;void SetExpired(){++expires;}};
struct CApplicationPlayer {
  CCriticalSection m_playerLock;
  std::shared_ptr<IPlayer> m_pPlayer;
  struct {
    std::shared_ptr<CFileItem> pItem;CPlayerOptions options;
    std::string playerName;IPlayerCallback* callback=nullptr;
  }m_nextItem;
  Timer m_audioStreamUpdate,m_videoStreamUpdate,m_subtitleStreamUpdate;
  int created=0;
  std::shared_ptr<IPlayer> GetInternal();
  bool CloseFile(bool=false);void ClosePlayer();void ResetPlayer();
  bool OpenFile(const CFileItem&,const CPlayerOptions&,const CPlayerCoreFactory&,
                const std::string&,IPlayerCallback&);
  void CreatePlayer(const CPlayerCoreFactory&,const std::string& name,IPlayerCallback&) {
    assert(!m_pPlayer);++created;m_pPlayer=std::make_shared<IPlayer>();
    m_pPlayer->m_name=name;m_pPlayer->applicationLock=&m_playerLock;
  }
};
'''
TESTS = r'''
int main(){
  CFileItem file;CPlayerOptions options;CPlayerCoreFactory factory;IPlayerCallback callback;
  for(bool playing:{false,true}) {
    CApplicationPlayer app;int destroyed=0;
    auto original=std::make_shared<IPlayer>();original->playing=playing;
    original->applicationLock=&app.m_playerLock;original->destroyed=&destroyed;
    app.m_pPlayer=original;
    assert(!app.OpenFile(file,options,factory,"new",callback));
    assert(app.GetInternal()==original&&original->closes==1&&app.created==0&&destroyed==0);
    // Main completion permits subsequent replacement; delayed failure did not.
    original->canClose=true;
    assert(app.OpenFile(file,options,factory,"new",callback));
    assert(app.GetInternal()!=original&&original->closes==2);
    assert(app.created==(playing?0:1));
    original.reset();assert(destroyed==1);
    app.ResetPlayer();
  }
  {CApplicationPlayer app;int destroyed=0;
   app.m_pPlayer=std::make_shared<IPlayer>();app.m_pPlayer->applicationLock=&app.m_playerLock;
   app.m_pPlayer->destroyed=&destroyed;
   app.ClosePlayer();assert(app.GetInternal()&&destroyed==0);
   app.m_pPlayer->canClose=true;app.ClosePlayer();assert(!app.GetInternal()&&destroyed==1);}
  // Exercise last-reference deletion in the actual ResetPlayer, including a
  // naturally stopped player removed by Application::OnPlayBackStopped.
  {CApplicationPlayer app;int destroyed=0;app.m_pPlayer=std::make_shared<IPlayer>();
   app.m_pPlayer->applicationLock=&app.m_playerLock;app.m_pPlayer->destroyed=&destroyed;
   app.ResetPlayer();assert(destroyed==1&&!app.GetInternal());}
}
'''


def main():
    source = (ROOT / 'xbmc/application/ApplicationPlayer.cpp').read_text()
    code = PRELUDE + '\n'.join(function(source, signature) for signature in [
        'std::shared_ptr<IPlayer> CApplicationPlayer::GetInternal()',
        'bool CApplicationPlayer::CloseFile(', 'void CApplicationPlayer::ClosePlayer()',
        'void CApplicationPlayer::ResetPlayer()', 'bool CApplicationPlayer::OpenFile(']) + TESTS
    with tempfile.TemporaryDirectory(prefix='player-retirement-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(code)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-pie', '-no-pie', str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Player retirement: PASS (five application methods; ASan/UBSan; recording media players)')


if __name__ == '__main__':
    main()
