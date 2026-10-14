#!/usr/bin/env python3
"""Exercise actual Home publication and Python request binding for PM4K startup."""
import argparse
import os
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = runpy.run_path(str(ROOT / 'tools/test-aml-native-continuation.py'))
function, run = FIXTURE['function'], FIXTURE['run']

BOUNDARY = r'''
#include "utils/PM4KStartup.h"
#include <algorithm>
#include <cassert>
#include <cctype>
#include <iostream>
#include <map>
#include <memory>
#include <vector>
using CCriticalSection=std::mutex;
constexpr int WINDOW_HOME=10000,TMSG_MEDIA_PLAY=1;
struct CVariant {
 std::string text;uint64_t token=0;
 CVariant()=default;CVariant(const char* s):text(s){}CVariant(uint64_t t):token(t){}
 std::string asString()const{return text;}
};
struct StringUtils {static bool EqualsNoCase(std::string a,std::string b){
 for(auto& c:a)c=std::tolower(static_cast<unsigned char>(c));
 for(auto& c:b)c=std::tolower(static_cast<unsigned char>(c));
 return a==b;
}};
struct CGUIWindow:CCriticalSection {
 int id=WINDOW_HOME;int GetID()const{return id;}
 std::map<std::string,CVariant>m_mapProperties;
 void SetProperty(const std::string&,const CVariant&);void ClearProperties();
};
@GUI@
struct CFileItem {
 std::string path;std::map<std::string,CVariant>properties;
 CFileItem(const std::string& p,bool):path(p){}void SetPath(const char* p){path=p;}
 void SetProperty(const char* p,const CVariant& v){properties[p]=v;}
};
struct CFileItemList {std::vector<std::shared_ptr<CFileItem>>items;void Add(std::shared_ptr<CFileItem>p){items.push_back(p);}};
struct Messenger {
 std::shared_ptr<CFileItem>last;
 void PostMsg(int,int a,int,void* p){if(a==0)last.reset(static_cast<CFileItem*>(p));else {std::unique_ptr<CFileItemList>l(static_cast<CFileItemList*>(p));last=l->items.at(0);}}
}messenger;
struct CServiceBroker{static Messenger*GetAppMessenger(){return &messenger;}};
struct CMediaSettings{static CMediaSettings& GetInstance(){static CMediaSettings s;return s;}void SetMediaStartWindowed(bool){}};
bool released=false;
struct Hook{std::string addon="script.plexmod";std::string GetAddonId(){assert(!released);return addon;}};
struct DelayedCallGuard{explicit DelayedCallGuard(Hook*){assert(!released);released=true;}~DelayedCallGuard(){released=false;}};
namespace AddonClass{template<class T>struct Ref{const T* p;explicit Ref(const T* v):p(v){}bool isSet()const{return p;}const T*operator->()const{return p;}};}
namespace xbmcgui{struct ListItem{std::shared_ptr<CFileItem>item=std::make_shared<CFileItem>("file",false);};}
using String=std::string;
#define XBMC_TRACE
struct Player{Hook*languageHook=nullptr;void playStream(const String&,const xbmcgui::ListItem*,bool);void playCurrent(bool){}};
@PLAYER@
int main(){
 CGUIWindow home,other;other.id=1;Hook hook;Player player;player.languageHook=&hook;xbmcgui::ListItem li;
 PM4K_STARTUP::Publish(false);
 player.playStream("native",&li,false);assert(messenger.last->properties[PM4K_STARTUP::ITEM_TOKEN].token==0);
 other.SetProperty("script.plex.playback_initializing","1");assert(!PM4K_STARTUP::Capture("script.plexmod"));
 home.SetProperty("SCRIPT.PLEX.PLAYBACK_INITIALIZING","1");
 const auto first=PM4K_STARTUP::Capture("script.plexmod");assert(first);
 hook.addon="other.addon";player.playStream("other",&li,false);assert(messenger.last->properties[PM4K_STARTUP::ITEM_TOKEN].token==0);
 hook.addon="script.plexmod";player.playStream("pm4k",&li,false);
 const auto token=messenger.last->properties[PM4K_STARTUP::ITEM_TOKEN].token;assert(token==first);
 assert(PM4K_STARTUP::Claim(token)==token&&PM4K_STARTUP::Pending(token));
 assert(PM4K_STARTUP::Claim(token)==0); // inherited/replayed item cannot reclaim
 home.SetProperty("script.plex.playback_initializing","");assert(!PM4K_STARTUP::Pending(token));
 home.SetProperty("script.plex.playback_initializing","1");const auto next=PM4K_STARTUP::Capture("script.plexmod");assert(next>token);
 home.SetProperty("script.plex.playback_initializing","1");const auto overwrite=PM4K_STARTUP::Capture("script.plexmod");assert(overwrite>next&&!PM4K_STARTUP::Pending(next));
 player.playStream("no-listitem",nullptr,false);assert(messenger.last->properties[PM4K_STARTUP::ITEM_TOKEN].token==overwrite);
 home.ClearProperties();assert(!PM4K_STARTUP::Pending(overwrite));
 const auto now=PM4K_STARTUP::Clock::now();PM4K_STARTUP::Publish(true,now);
 const auto timed=PM4K_STARTUP::Capture("script.plexmod",now);assert(PM4K_STARTUP::Claim(timed,now)==timed);
 assert(PM4K_STARTUP::Pending(timed,now+PM4K_STARTUP::MAX_WAIT-std::chrono::milliseconds(1)));
 assert(!PM4K_STARTUP::Pending(timed,now+PM4K_STARTUP::MAX_WAIT));
 assert(!PM4K_STARTUP::Capture("script.plexmod",now+PM4K_STARTUP::MAX_WAIT));
 PM4K_STARTUP::Publish(true,now);const auto orphan=PM4K_STARTUP::Capture("script.plexmod",now);
 assert(!PM4K_STARTUP::Claim(orphan,now+PM4K_STARTUP::MAX_WAIT));
 player.languageHook=nullptr;player.playStream("native-reuse",&li,false);assert(!messenger.last->properties[PM4K_STARTUP::ITEM_TOKEN].token);
 PM4K_STARTUP::Publish(false);
 std::cout<<"PASS: actual Home publication/clear and Python play ownership, pre-release interpreter access, missing/native/stale/replay/overwrite/timeout\n";
}
'''


def source():
    gui = (ROOT / 'xbmc/guilib/GUIWindow.cpp').read_text()
    player = (ROOT / 'xbmc/interfaces/legacy/Player.cpp').read_text()
    methods = '\n'.join(function(gui, marker) for marker in ('void CGUIWindow::SetProperty(', 'void CGUIWindow::ClearProperties()'))
    return BOUNDARY.replace('@GUI@', methods).replace('@PLAYER@', function(player, 'void Player::playStream('))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    os.environ.setdefault('ASAN_OPTIONS', 'detect_leaks=0')
    code = source()
    run(code)
    if args.negative_controls:
        header = (ROOT / 'xbmc/utils/PM4KStartup.h').read_text()
        for label, old, new in [
            ('wrong addon owns signal', 'if (addon != "script.plexmod")', 'if (false && addon != "script.plexmod")'),
            ('replayed token owns later native item', 'token <= signal.consumed ||', 'false ||'),
            ('orphan signal never expires', 'now - signal.published < MAX_WAIT;', 'now - signal.published < MAX_WAIT * 100;'),
        ]:
            assert old in header, label
            run(code, ('utils/PM4KStartup.h', header.replace(old, new)), negative=True)
            print('REJECTED:', label)


if __name__ == '__main__':
    main()
