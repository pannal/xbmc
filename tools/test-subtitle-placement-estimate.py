#!/usr/bin/env python3
"""Subtitle-only photometry, error limits and connected publication/placement.

Hash-pinned native frames remain optional external inputs. The hybrid seven-frame
fixture does not establish film acceptance. Native L5 and public picture geometry
must never consume these estimates, including after a pure seek.
"""
import argparse
import hashlib
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
PIXEL = runpy.run_path(str(ROOT / 'tools/test-subtitle-pixel-edges.py'))
TEXT = PIXEL['TEXT']

TESTS = r'''
Result subtitleResult(const Result& strict){
#ifdef SUBTITLE_EDGE_ESTIMATE
 (void)strict;const auto& m=subtitleEdgeMeasurements;
 return {m[0].value,m[1].value,m[2].value,m[3].value,
  std::any_of(m.begin(),m.end(),[](const auto& e){return e.RejectedSpatialEvidence();})};
#else
 return strict;
#endif
}
Result placement(PixelImage& image){return subtitleResult(image.edges());}
bool placementSelect(const std::vector<Result>& edges,DetectResult& result){
 uint16_t t[7],b[7],l[7],r[7];bool rejected[7];assert(edges.size()<=7);
 for(size_t i=0;i<edges.size();++i){t[i]=edges[i].top;b[i]=edges[i].bottom;l[i]=edges[i].left;r[i]=edges[i].right;rejected[i]=edges[i].rejected;}
#ifdef SUBTITLE_EDGE_ESTIMATE
 return detect_subtitle_consensus(t,b,l,r,rejected,edges.size(),result);
#else
 return Select(t,b,l,r,edges.size(),1920,1080,result,rejected);
#endif
}
DetectResult nativeEstimate(){
#ifdef NATIVE_NINE_FRAMES
 auto strict45=readNativeFrame(nativeFramePaths[0]);auto sub45=subtitleResult(strict45);
 assert(strict45.top==Unobserved&&strict45.bottom==Unobserved&&strict45.rejected);
 assert(sub45.top==20&&sub45.bottom==20&&!sub45.rejected);
 auto strict30=readNativeFrame(nativeFramePaths[1]);auto sub30=subtitleResult(strict30);
 assert(strict30.top==20&&strict30.bottom==20&&!strict30.rejected);
 assert(sub30.top==20&&sub30.bottom==20&&!sub30.rejected);
#else
 PixelImage realSubstitute;realSubstitute.fill(20,20,0,0,90,90,20,20);
 auto sub45=placement(realSubstitute),sub30=sub45;
#endif
 DetectResult result{};assert(!placementSelect({sub45,sub30},result));
 PixelImage image;std::vector<Result> hybrid;
 for(int i=0;i<6;++i){image.fill(20,20,0,0,26+i%3,50+i*3,123+i*9,223+i*7);hybrid.push_back(placement(image));}
 hybrid.push_back(sub45);assert(placementSelect(hybrid,result)&&result.top==20&&result.bottom==20&&result.left==0&&result.right==0);
 hybrid[0]=sub30;assert(placementSelect(hybrid,result)&&result.top==20&&result.bottom==20);
 const auto accepted=result;
 image.fill(40,40,0,0);hybrid[1]=placement(image);assert(!placementSelect(hybrid,result));
 image.fill(0,0,0,0,80,90,0,0);for(int x=928;x<992;++x){image.bytes[x]=120;image.bytes[1079*1920+x]=120;}
 hybrid[1]=placement(image);assert(hybrid[1].top==0&&hybrid[1].bottom==0&&!hybrid[1].rejected);assert(!placementSelect(hybrid,result));
 // Tolerance never permits moving inward past any observed smaller side.
 hybrid.assign(7,Result{20,20,20,20,false});hybrid[5]={100,18,100,17,false};hybrid[6]={19,100,16,100,false};
 assert(placementSelect(hybrid,result)&&result.top==19&&result.bottom==18&&result.left==16&&result.right==17);
#ifdef NATIVE_NINE_FRAMES
 std::cout<<"PASS: actual native45/30 placement at20; strict45 rejected; ";
#else
 std::cout<<"PASS: synthetic placement at20; ";
#endif
 std::cout<<"two samples insufficient; explicit hybrid confidence, framing vetoes and four-side no-inward bound\n";
 return accepted;
}
void photometricLimits(){
 PixelImage image;DetectResult result{};
 for(int level:{24,50,90,200})for(int end:{21,28,39}){
  image.fill(40,40,0,0,90,90,40,40);
  for(int depth=20;depth<end;++depth)for(int x=0;x<1920;++x){image.bytes[depth*1920+x]=level;image.bytes[(1079-depth)*1920+x]=level;}
  auto e=placement(image);assert(e.top==Unobserved&&e.bottom==Unobserved&&e.rejected);
 }
 for(int level:{16,18,20,22}){
  image.fill(40,40,0,0,90,90,40,40);
  for(int depth=20;depth<40;++depth)for(int x=0;x<1920;++x){int value=depth<28?24:level;image.bytes[depth*1920+x]=value;image.bytes[(1079-depth)*1920+x]=value;}
  auto e=placement(image);assert(e.top==Unobserved&&e.bottom==Unobserved&&e.rejected);
 }
 for(int flash:{19,20}){
  image.fill(40,40,0,0,90,90,40,40);
  for(int depth=20;depth<40;++depth)for(int x=0;x<1920;++x)if(x<900||x>=1020)image.bytes[(1079-depth)*1920+x]=90;
  for(int x=928;x<992;++x)image.bytes[(1079-flash)*1920+x]=24;
  auto e=placement(image);assert(e.top==40&&e.bottom==Unobserved&&e.rejected);
 }
 // Explicit accepted ambiguity: same pixels can be side graphics in true40
 // bars or dim content in true20 framing. Only placement may use this estimate.
 for(int level:{18,20,22}){
  image.fill(40,40,0,0,90,90,40,40);
  for(int depth=20;depth<40;++depth){
   for(int x=0;x<1920;++x)if(x<900||x>=1020)image.bytes[(1079-depth)*1920+x]=90;
   for(int x=928;x<992;++x)image.bytes[(1079-depth)*1920+x]=depth==20?24:level;
  }
  auto strict=image.edges(),e=subtitleResult(strict);
  assert(strict.rejected&&strict.bottom==Unobserved);assert(e.top==40&&e.bottom==20&&!e.rejected);
  std::vector<Result> samples(7,Result{20,20,0,0,false});samples[6]=strict;assert(!sampleSelect(samples,1920,1080,result));
  samples[6]=e;assert(placementSelect(samples,result)&&result.top==20&&result.bottom==20);
 }
 std::cout<<"PASS: 12 stripes, four raised returns, two centre flashes rejected; three explicitly accepted graphic ambiguities remain unsafe for native geometry\n";
}
bool estimatedGet(uint16_t expected=20){uint16_t t,b,l,r;bool found=aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true);if(found)assert(t==expected&&b==expected&&l==0&&r==0);return found;}
void onlyEstimate(const DetectResult& result){CAMLNativeWorker::Run run(s_detectSource->superseded);detect_publish_subtitle(run,s_detectSource,result);}
void awaitEstimate(){
 auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
 for(;;){
  {std::lock_guard<std::mutex> lock(s_detectSourceMutex);if(s_detectSource->subtitleResult&&s_detectState!=DV_DETECT_RUNNING)return;}
  assert(std::chrono::steady_clock::now()<deadline);std::this_thread::sleep_for(std::chrono::milliseconds(1));
 }
}
void isolation(const char* font,const DetectResult& result){
 for(bool native:{false,true}){
  select(native,1920,1080);cache.meta={};s_detectState=DV_DETECT_SKIPPED;s_detectStable=false;s_detectInjected=false;
  const auto before=writes.load();onlyEstimate(result);
  uint16_t t,b,l,r;assert(!s_detectSource->result&&estimatedGet());
  assert(!aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r)&&t==0&&b==0);
  aml_dv_detect_active_area_get(t,b,l,r);assert(t==0&&b==0&&l==0&&r==0);
  assert(!aml_dv_detect_active_area_stable()&&s_detectState==DV_DETECT_SKIPPED&&writes==before&&!s_detectInjected);
  CRenderManager manager;manager.m_picture.iWidth=1920;manager.m_picture.iHeight=1080;
  manager.m_picture.hdrType=native?StreamHdrType::HDR_TYPE_DOLBYVISION:StreamHdrType::HDR_TYPE_NONE;
  auto& overlay=manager.m_overlays;overlay.m_rs={0,0,1920,1080};
  rect(manager.CalcPictureActiveArea(overlay.m_rs,overlay.m_rd,overlay.m_rv,500000,true),0,1080);
  const auto signal=renderSignalArea(manager,overlay.m_rs,overlay.m_rd,overlay.m_rv,500000);
  rect(signal,0,1080);
  const auto placementArea=manager.CalcPictureActiveArea(overlay.m_rs,overlay.m_rd,overlay.m_rv,500000,false,true);
  for(bool restricted:{false,true})assert(TextSubtitleNeedsSignal(restricted,placementArea,signal,overlay.m_rv,1050));
  auto c=cue(font,"Dark picture placement gypq\\NSecond line descenders");auto st=makeStyle();
  auto raw=draw(manager,c,st,false,true);auto placed=draw(manager,c,st);
  rect(overlay.m_activePicture,20,1060);inside(placed.bounds,overlay.m_activePicture);assert(placed.bounds.bottom<=1060);
  assert(placed.bounds.bottom<=raw.bounds.bottom);auto paused=draw(manager,c,st);assert(paused.canvas==placed.canvas);
  if(native){
   cache.pts=500000;cache.meta.has_level5_metadata=true;cache.meta.level5_active_area_top_offset=cache.meta.level5_active_area_bottom_offset=100;
   rect(manager.CalcOverlayActiveArea(overlay.m_rs,overlay.m_rd,overlay.m_rv,true,500000),100,980);
   overrideActive=true;overrideTop=overrideBottom=0;rect(manager.CalcOverlayActiveArea(overlay.m_rs,overlay.m_rd,overlay.m_rv,true,500000),0,1080);overrideActive=false;cache.meta={};
  }
  // Retain separately through repeated pure seeks; only native startup clears
  // may write, never restoration of the permissive geometry itself.
  auto selected=aml_subtitle_active_area_source();auto old=s_detectSource;CAMLNativeWorker::Run stale(old->superseded);
  Invalidate(true);Invalidate(true);assert(estimatedGet()&&!old->retained&&old->retainedSubtitle);
  assert(aml_subtitle_active_area_configure(1920,1080,native,true,selected));
  auto scanCount=scans.load();aml_dv_detect_active_area_start();awaitEstimate();s_detectWorker.Stop();
  assert(scans==scanCount&&estimatedGet()&&!s_detectSource->result&&!s_detectStable&&s_detectState==DV_DETECT_SKIPPED);
  assert(writes==before+(native?4:0)&&!s_detectInjected);
  detect_publish_subtitle(stale,old,DetectResult{100,100,0,0});assert(!old->subtitleResult&&estimatedGet());
  rect(manager.CalcPictureActiveArea(overlay.m_rs,overlay.m_rd,overlay.m_rv,500000,true),0,1080);
  // Strict zero is authoritative, as is strict positive geometry.
  CAMLNativeWorker::Run current(s_detectSource->superseded);detect_publish(current,s_detectSource,0,0,0,0);assert(estimatedGet(0));
  detect_publish(current,s_detectSource,30,30,0,0);assert(estimatedGet(30));
 }
 for(int change=0;change<5;++change){
  select(false,1920,1080);onlyEstimate(result);auto selected=aml_subtitle_active_area_source();auto old=s_detectSource;CAMLNativeWorker::Run stale(old->superseded);
  Invalidate(true);assert(estimatedGet());
  int width=change==0?3840:1920,height=change==0?2160:1080;bool native=change==1,probe=change!=2;
  if(change==3)Invalidate();
  if(change==4){aml_dv_detect_set_file("fixture-same-path");assert(!aml_subtitle_active_area_configure(width,height,native,probe,selected));}
  else assert(aml_subtitle_active_area_configure(width,height,native,probe,selected));
  uint16_t t,b,l,r;assert(!aml_subtitle_detect_active_area_get(width,height,t,b,l,r,true));
  detect_publish_subtitle(stale,old,result);assert(!old->subtitleResult);
 }
 select(false,1920,1080);onlyEstimate(result);CVideoPlayerVideo owner;owner.m_hints.subtitleProbeSource=aml_subtitle_active_area_source();
 owner.m_subtitleProbeSource=owner.m_hints.subtitleProbeSource;
 owner.m_hints.width=1920;owner.m_hints.height=1080;auto codec=std::make_unique<CDVDVideoCodecFFmpeg>();codec->accel=true;owner.m_pVideoCodec=std::move(codec);
 VideoPicture picture;picture.iWidth=1920;picture.iHeight=1080;owner.UpdateSubtitleProbe(picture);assert(!estimatedGet());
 std::cout<<"PASS: connected subtitle-only libass bounds20..1060; no native/public/signal geometry, strict-zero precedence, warm-cache/state, authored/manual and source/lifecycle/HW isolation\n";
}
int main(int argc,char** argv){
 assert(argc==2);auto result=nativeEstimate();photometricLimits();ready();isolation(argv[1],result);s_detectWorker.Stop();
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ass-include', type=Path, required=True)
    parser.add_argument('--ass-library', required=True)
    parser.add_argument('--font', type=Path, default=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
    parser.add_argument('--native-nine-frames', type=Path)
    parser.add_argument('--baseline', help='Strict estimator revision; must fail the native 45%% subtitle assertion')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    revision, args.baseline = args.baseline, None
    frames = None
    if args.native_nine_frames:
        fixtures = [('nine-45pct-pts2144309.pgm', '23c90045e9a2665b2bfa71a4f1c8d14952d6c29ae80bfb1fbe4447b811ba222a'),
                    ('nine-30pct-pts1430095.pgm', '033c80b04558d53a4d021165c19bd6489db56a5665abe99fd5c3c1c7ba288c1b')]
        frames = [args.native_nine_frames / name for name, _ in fixtures]
        for path, (_, digest) in zip(frames, fixtures):
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    else:
        assert not revision, 'Native frames required for the parent comparison'
        print('Native frames not supplied; only synthetic placement limits exercised.', flush=True)
    code = PIXEL['generate'](revision, frames).replace('int main(int argc,char** argv)', 'void retainedPixelMain(int argc,char** argv)', 1)
    manager = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp').read_text()
    render = TEXT['FUNCTION'](manager, 'void CRenderManager::Render(')
    signal = render[render.index('      const CRect signalActive ='):].split(';', 1)[0] + ';'
    assert 'TextSubtitleNeedsSignal(restrictSubsToActiveArea, active, signalActive,' in render
    assert 'HasImageSubOutsideActiveArea(prepared, signalActive)' in render
    code += '\n' + TEXT['FUNCTION'](manager, 'bool TextSubtitleNeedsSignal(')
    code += '\nCRect renderSignalArea(CRenderManager& manager,const CRect& src,const CRect& dst,const CRect& view,double pts){' + signal.replace('CalcPictureActiveArea(', 'manager.CalcPictureActiveArea(').replace('frame->present.pts', 'pts') + 'return signalActive;}\n'
    code += TESTS
    result = TEXT['run'](code, args, negative=bool(revision))
    if revision:
        assert 'sub45.top==20&&sub45.bottom==20&&!sub45.rejected' in result.stderr, result.stderr
        print('REJECTED strictly compiled parent at native 45% subtitle assertion:', revision)
    elif args.negative_controls:
        for label, old, new in [
            ('estimate promoted to strict result', 'source->subtitleResult = std::make_shared<const DetectResult>(result);', 'source->result = std::make_shared<const DetectResult>(result);'),
            ('public snapshot opts into estimate', 'right, allowSubtitleEstimate);', 'right, true || allowSubtitleEstimate);'),
            ('masking signal trusts estimate', 'manager.CalcPictureActiveArea(src, dst, view, pts, false);', 'manager.CalcPictureActiveArea(src, dst, view, pts, false, true);'),
            ('restriction mistaken for native clearance', 'if (placement != active)', 'if (false && placement != active)'),
            ('strict zero erased by estimate', 'if (!accepted && allowSubtitleEstimate)', 'if (allowSubtitleEstimate)'),
            ('warm cache promoted to native', 'detect_publish_subtitle(run, source, *retainedSubtitle);', 'detect_publish(run, source, retainedSubtitle->top, retainedSubtitle->bottom, retainedSubtitle->left, retainedSubtitle->right);'),
            ('destructive subtitle cache survives', 's_detectSource->retainedSubtitle.reset();', '(void)preserveGeometry;'),
            ('measured-black returns ignored', 'depth > result.departures[lane] && levels[lane] <= blackMax[lane] + strip', 'false && depth > result.departures[lane] && levels[lane] <= blackMax[lane] + strip'),
            ('whole-picture evidence lost', 'if (!strong)', 'if (false && !strong)'),
            ('inward temporal tolerance allowed', 'result.top = std::min(result.top, top[i]);', '(void)top[i];'),
        ]:
            assert old in code, label
            TEXT['run'](code.replace(old, new, 1), args, negative=True)
            print('REJECTED compiled placement control:', label)


if __name__ == '__main__':
    main()
