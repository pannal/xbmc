#!/usr/bin/env python3
"""Production PlayMedia preparation + playlist request transfer.

Resolver, GUI, playlist expansion and player opening are recording boundaries.
Checks metadata writeback, one-shot ownership and reusable-entry cleanup.
"""
import argparse
import os
from pathlib import Path
import runpy
ROOT=Path(__file__).resolve().parents[1]
F=runpy.run_path(str(ROOT/'tools/test-aml-native-continuation.py'))
function,run=F['function'],F['run']
PREFIX=r'''
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-parameter"
#include "utils/Variant.cpp"
#pragma GCC diagnostic pop
#include "utils/DetectorAdmission.h"
#include <cassert>
#include <chrono>
#include <iostream>
#include <memory>
#include <optional>
#include <vector>
struct CFileItem {
 CFileItem()=default;explicit CFileItem(const std::shared_ptr<CFileItem>&){}
 std::string path="file.mkv",mime;bool audio=false,plugin=false,fail=false,list=false,pvr=false;
 std::map<std::string,CVariant>props;int resolved=0;
 bool HasProperty(const char* k)const{return props.count(k);}
 CVariant GetProperty(const char* k)const{auto p=props.find(k);return p==props.end()?CVariant():p->second;}
 void SetProperty(const char* k,const CVariant& v){props[k]=v;}void ClearProperty(const char* k){props.erase(k);}
 bool IsAudio()const{return audio;}bool IsVideo()const{return !audio;}bool IsGame()const{return false;}bool IsPicture()const{return false;}
 bool IsPlayList()const{return list;}bool IsSmartPlayList()const{return false;}bool IsInternetStream()const{return false;}bool IsPVR()const{return pvr;}
 const auto& GetPath()const{return path;}const auto& GetURL()const{return path;}std::string GetLabel()const{return "test";}
};
struct CFileItemList{int Size()const{return 0;}};
struct CURL{explicit CURL(const std::string&){}std::string GetProtocol()const{return "file";}std::string GetHostName()const{return {};}};
namespace PLAYLIST {enum Id{TYPE_NONE,TYPE_MUSIC,TYPE_VIDEO};struct CPlayList{std::vector<std::shared_ptr<CFileItem>> items;void Add(const CFileItemList&){}size_t size()const{return items.size();}auto operator[](size_t n){return items.at(n);}};}
struct URIUtils{static bool HasPluginPath(const CFileItem& i){return i.plugin;}};
namespace XFILE{struct CPluginDirectory{static bool GetResolvedPluginResult(CFileItem& i){++i.resolved;i.mime="video/resolved";return !i.fail;}};}
struct CUtil{static void GetRecursiveListing(const std::string&,CFileItemList&,const char*,int){}};
struct CSmartPlaylist{void OpenAndReadName(const std::string&){}std::string GetType()const{return "movies";}std::string GetName()const{return "test";}};
struct Localize{std::string Get(int){return {};}}g_localizeStrings;
using namespace std::chrono_literals;
constexpr int DIR_FLAG_NO_FILE_DIRS=0,LOGWARNING=1;
struct CLog{template<class... T>static void Log(T...) {}};
struct CGUIDialogCache{CGUIDialogCache(std::chrono::seconds,const std::string&,const std::string&){}void Close(){}bool IsCanceled(){return false;}};
std::shared_ptr<CFileItem> selected=std::make_shared<CFileItem>();
struct CCreateAndLoadPlayList{CCreateAndLoadPlayList(CFileItem& i,std::unique_ptr<PLAYLIST::CPlayList>& p){if(i.list){p=std::make_unique<PLAYLIST::CPlayList>();p->items.push_back(selected);}}};
struct CGUIDialogBusy{static bool Wait(CCreateAndLoadPlayList*,int,bool){return true;}};
namespace PVR::GUI{struct Playback{bool PlayMedia(const CFileItem&);};}
struct PVRManager{template<class T>T Get(){return {};}};
using AddonPtr=std::shared_ptr<CFileItem>;
namespace ADDON{enum AddonType{GAMEDLL};struct OnlyEnabled{enum CHOICE{CHOICE_YES};};}
using ADDON::AddonType;using ADDON::OnlyEnabled;
struct AddonManager{bool GetAddon(const std::string&,AddonPtr&,ADDON::AddonType,ADDON::OnlyEnabled::CHOICE){return false;}};
struct CServiceBroker{static PVRManager GetPVRManager(){return {};}static AddonManager GetAddonMgr(){return {};}};
struct CApplication {
 std::vector<CFileItem> requests;std::optional<bool> playlistDecision;
 bool PlayMedia(CFileItem&,const std::string&,PLAYLIST::Id);
 bool PlayFile(CFileItem i,const std::string&,bool){DETECTOR_ADMISSION::CaptureItem(i);requests.push_back(i);return true;}
 bool ProcessAndStartPlaylist(const std::string&,PLAYLIST::CPlayList&,PLAYLIST::Id,int track=0,std::optional<bool> skip=std::nullopt){assert(track==0);playlistDecision=skip;return true;}
}g_application;
bool PVR::GUI::Playback::PlayMedia(const CFileItem& i){return g_application.PlayFile(i,"",false);}
@PLAYMEDIA@
bool playlistAttempt(const std::shared_ptr<CFileItem>& item,std::optional<bool> detectorSkip){
 const std::string player;const bool bAutoPlay=false;
 @TRANSFER@
 return ret;
}
int main(){
 CFileItem entry;entry.plugin=true;
 DETECTOR_ADMISSION::Publish("1");assert(g_application.PlayMedia(entry,"",PLAYLIST::TYPE_VIDEO));
 assert(entry.resolved==1&&entry.mime=="video/resolved");assert(!entry.HasProperty(DETECTOR_ADMISSION::ITEM_SKIP));
 assert(g_application.requests.back().GetProperty(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean());
 assert(DETECTOR_ADMISSION::Value().empty());
 assert(g_application.PlayMedia(entry,"",PLAYLIST::TYPE_VIDEO));assert(entry.resolved==2);
 assert(!g_application.requests.back().GetProperty(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean());
 // A queued false snapshot survives a newer publication during preparation.
 entry.SetProperty(DETECTOR_ADMISSION::ITEM_SKIP,false);DETECTOR_ADMISSION::Publish("1");
 assert(g_application.PlayMedia(entry,"",PLAYLIST::TYPE_VIDEO));
 assert(!g_application.requests.back().GetProperty(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean());assert(DETECTOR_ADMISSION::Value()=="1");
 // A failed candidate consumes its flag; subsequent playback does not inherit it.
 entry.fail=true;assert(!g_application.PlayMedia(entry,"",PLAYLIST::TYPE_VIDEO));assert(DETECTOR_ADMISSION::Value().empty());
 entry.fail=false;assert(g_application.PlayMedia(entry,"",PLAYLIST::TYPE_VIDEO));assert(!g_application.requests.back().GetProperty(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean());
 // Playlist containers defer consumption; captured container requests propagate.
 CFileItem container;container.list=true;DETECTOR_ADMISSION::Publish("1");
 assert(g_application.PlayMedia(container,"",PLAYLIST::TYPE_VIDEO));assert(!g_application.playlistDecision.has_value()&&DETECTOR_ADMISSION::Value()=="1");
 container.SetProperty(DETECTOR_ADMISSION::ITEM_SKIP,DETECTOR_ADMISSION::Capture());
 assert(g_application.PlayMedia(container,"",PLAYLIST::TYPE_VIDEO));assert(g_application.playlistDecision==true&&!container.HasProperty(DETECTOR_ADMISSION::ITEM_SKIP));
 selected->SetProperty(DETECTOR_ADMISSION::ITEM_SKIP,true);assert(playlistAttempt(selected,std::nullopt));
 assert(g_application.requests.back().GetProperty(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean()&&!selected->HasProperty(DETECTOR_ADMISSION::ITEM_SKIP));
 assert(playlistAttempt(selected,std::nullopt));assert(!g_application.requests.back().GetProperty(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean());
 DETECTOR_ADMISSION::Publish("1");assert(playlistAttempt(selected,false));assert(!g_application.requests.back().GetProperty(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean()&&DETECTOR_ADMISSION::Value()=="1");
 std::cout<<"PASS: actual PlayMedia metadata writeback, failed/repeated preparation, queued false ownership and actual reusable playlist request transfer\n";
}
'''

def source():
    app=(ROOT/'xbmc/application/Application.cpp').read_text()
    playlist=(ROOT/'xbmc/PlayListPlayer.cpp').read_text()
    body=function(playlist,'bool CPlayListPlayer::Play(int iSong,')
    a=body.index('  // A submitted request owns its snapshot;')
    b=body.index('  // OpenFile can release GUI ownership',a)
    return PREFIX.replace('@PLAYMEDIA@',function(app,'bool CApplication::PlayMedia(')).replace('@TRANSFER@',body[a:b])


def main():
    p=argparse.ArgumentParser();p.add_argument('--negative-controls',action='store_true');a=p.parse_args()
    os.environ.setdefault('ASAN_OPTIONS','detect_leaks=0')
    code=source();run(code)
    if a.negative_controls:
        original='bool CApplication::PlayMedia(CFileItem& item,'
        replacement='bool CApplication::PlayMedia(CFileItem& inputItem,'
        assert original in code
        broken=code.replace(original,replacement,1).replace('  // Preserve the existing resolver/MIME updates', '  CFileItem item(inputItem);\n  // Preserve the existing resolver/MIME updates',1)
        run(broken,negative=True);print('REJECTED: resolver writeback suppressed')
        for label,old,new in [
            ('playlist snapshot persists','item->ClearProperty(DETECTOR_ADMISSION::ITEM_SKIP);','/* old decision retained */'),
            ('queued false overwritten','if (detectorSkip)\n    request.SetProperty','if (detectorSkip && *detectorSkip)\n    request.SetProperty'),
        ]:
            assert old in code,label
            broken=code.replace(old,new)
            run(broken,negative=True);print('REJECTED:',label)
if __name__=='__main__':main()
