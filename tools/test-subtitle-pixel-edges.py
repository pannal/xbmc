#!/usr/bin/env python3
"""Production pixel extraction -> consensus/snap -> publication -> real libass bounds.

Synthetic controls and optional hash-pinned native frames cover measured edges.
Two native frames do not establish seven-position film acceptance.
Decode/VFS and GPU are boundaries, as in the retained connected fixture.
"""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DETECTOR = runpy.run_path(str(ROOT / 'tools/test-subtitle-active-area-detection.py'))
TEXT = runpy.run_path(str(ROOT / 'tools/test-subtitle-text-integration.py'))


def generate(baseline=None, native_frames=None):
    aml = TEXT['source']('xbmc/utils/AMLUtils.cpp', baseline)
    pixel = DETECTOR['source'](aml)
    prefix = DETECTOR['PREFIX']
    formats = prefix[prefix.index('constexpr int AV_PIX_FMT'):prefix.index('constexpr int DV_DETECT')]
    frame = prefix[prefix.index('struct Frame '):prefix.index('struct DetectResult ')]
    bodies = pixel[pixel.index('bool AdmitFrame('):pixel.index('DOVIFrameMetadata CDataCacheCore::')]
    # Renderer/lifecycle code stays at the current version. Only the pixel
    # estimator changes in the original-versus-corrected comparison.
    connected = TEXT['generate']().replace('int main(int argc,char** argv)', 'void retainedMain(int argc,char** argv)', 1)
    native = ''
    if native_frames:
        native = '#include <fstream>\n#define NATIVE_NINE_FRAMES 1\nstatic const char* nativeFramePaths[]={' + ','.join(json.dumps(str(path)) for path in native_frames) + '};\n'
    return connected + formats + frame + '\nstruct Result {uint16_t top,bottom,left,right;bool rejected{false};};\nconstexpr int DV_DETECT_SKIP_IMAX=6;\n' + bodies + native + TESTS


TESTS = r'''
struct PixelImage {
 Frame frame;Source source;std::vector<uint8_t> bytes;
 PixelImage(int width=1920,int height=1080):bytes(width*height,16){
  frame.width=source.width=width;frame.height=source.height=height;frame.linesize[0]=width;frame.data[0]=bytes.data();}
 void fill(int top,int bottom,int left,int right,int shoulder=26,int middle=66,int deepTop=123,int deepBottom=223){
  std::fill(bytes.begin(),bytes.end(),16);
  for(int y=top;y<frame.height-bottom;++y)for(int x=left;x<frame.width-right;++x){
   int value=shoulder;
   if(x>=frame.width/2-32&&x<frame.width/2+32)
    value=(y<deepTop||y>=frame.height-deepBottom)?24:middle;
   bytes[y*frame.width+x]=static_cast<uint8_t>(value);
  }
 }
 Result edges(){assert(AdmitFrame(&frame,&source));assert(Contrast(&frame)>=10);return Edges(&frame);}
};
bool sampleSelect(const std::vector<Result>& edges,int width,int height,DetectResult& result){
 assert(edges.size()==7);uint16_t t[7],b[7],l[7],r[7];bool rejected[7];
 for(int i=0;i<7;++i){t[i]=edges[i].top;b[i]=edges[i].bottom;l[i]=edges[i].left;r[i]=edges[i].right;rejected[i]=edges[i].rejected;}
 return Select(t,b,l,r,7,width,height,result,rejected);
}
DetectResult pixels(){
 PixelImage image;std::vector<Result> scans;DetectResult result{};
 for(int i=0;i<7;++i){
  image.fill(20,20,0,0,26+i%3,50+i*3,123+i*9,223+i*7);
  // Compressed/noisy near-black does not depend on a perfectly constant16.
  for(int y=0;y<20;++y)for(int x=0;x<1920;++x){
   image.bytes[y*1920+x]=static_cast<uint8_t>(14+(x+y+i)%5);
   image.bytes[(1079-y)*1920+x]=static_cast<uint8_t>(14+(x+y+i)%5);
  }
  scans.push_back(image.edges());
 }
 assert(sampleSelect(scans,1920,1080,result)&&result.top==21&&result.bottom==21&&result.left==0&&result.right==0);
 for(const auto& edge:scans)assert(edge.top==20&&edge.bottom==20&&edge.left==0&&edge.right==0);
 const auto accepted=result;
 // Replacing one sample with actual wider/narrower/full-frame picture keeps
 // the original smaller-bound and bilateral inward framing vetoes effective.
 image.fill(0,0,0,0,80,90,0,0);
 for(int x=928;x<992;++x){image.bytes[x]=120;image.bytes[1079*1920+x]=120;}
 scans[6]=image.edges();
 assert(scans[6].top==0&&scans[6].bottom==0&&!scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 image.fill(10,10,0,0);scans[6]=image.edges();assert(!sampleSelect(scans,1920,1080,result));
 image.fill(40,40,0,0);scans[6]=image.edges();assert(scans[6].top==40&&scans[6].bottom==40);assert(!sampleSelect(scans,1920,1080,result));
 // A small isolated caption in a bar outside the old centre strip is veto
 // evidence, not something to discard for failing the spatial quorum.
 image.fill(20,20,0,0);
 for(int x=0;x<64;++x)image.bytes[7*1920+x]=200;
 scans[6]=image.edges();assert(scans[6].top==Unobserved&&scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 image.fill(20,20,0,0);
 for(int y=7;y<20;++y)for(int x=928;x<992;++x)image.bytes[y*1920+x]=24;
 scans[6]=image.edges();assert(scans[6].top==Unobserved&&scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 // A persistent isolated earlier departure still cannot be overruled by
 // the other strips' majority at20 or by a deep original centre estimate.
 image.fill(20,20,0,0);
 for(int y=7;y<20;++y)for(int x=0;x<64;++x)image.bytes[y*1920+x]=200;
 scans[6]=image.edges();assert(scans[6].top==Unobserved&&scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 // Broad captions/graphics can meet the spatial quorum on both sides of
 // a genuinely larger bar. Their return to black must retain a framing veto.
 image.fill(40,40,0,0,90,90,40,40);
 for(int x=1200;x<1920;++x)image.bytes[1059*1920+x]=200;
 scans[6]=image.edges();assert(scans[6].top==40&&scans[6].bottom==Unobserved&&scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 // Dim full-width stripes evade the old midpoint but include the centre
 // as well as distributed strips; return-to-black remains an independent gate.
 image.fill(40,40,0,0,90,90,40,40);
 for(int y=20;y<28;++y)for(int x=0;x<1920;++x){image.bytes[y*1920+x]=24;image.bytes[(1079-y)*1920+x]=24;}
 scans[6]=image.edges();assert(scans[6].top==Unobserved&&scans[6].bottom==Unobserved&&scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 image.fill(40,40,0,0,90,90,40,40);
 for(int x=0;x<760;++x)image.bytes[20*1920+x]=200;
 for(int x=1200;x<1920;++x)image.bytes[1059*1920+x]=200;
 scans[6]=image.edges();assert(scans[6].top==Unobserved&&scans[6].bottom==Unobserved&&scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 // A broad graphic may persist all the way to the actual image boundary.
 // It must not turn a genuine larger pair into a tolerated unilateral outlier.
 image.fill(40,40,0,0,90,90,40,40);
 for(int y=20;y<40;++y)for(int x=1200;x<1920;++x)image.bytes[(1079-y)*1920+x]=200;
 scans[6]=image.edges();assert(scans[6].top==40&&scans[6].bottom==Unobserved&&scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 // The same safety decision covers thicker graphics and compressed stripes.
 image.fill(40,40,0,0,90,90,40,40);
 for(int y=20;y<28;++y){
  for(int x=0;x<760;++x)image.bytes[y*1920+x]=200;
  for(int x=1200;x<1920;++x)image.bytes[(1079-y)*1920+x]=200;
 }
 scans[6]=image.edges();assert(scans[6].top==Unobserved&&scans[6].bottom==Unobserved&&scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 // A bright outer strip is direct full-frame evidence; a dark/low-contrast
 // frame does not acquire usable evidence through the refinement.
 image.fill(20,20,0,0);for(int x=0;x<64;++x)image.bytes[x]=100;
 scans[6]=image.edges();assert(scans[6].top==0&&!scans[6].rejected);assert(!sampleSelect(scans,1920,1080,result));
 std::fill(image.bytes.begin(),image.bytes.end(),16);assert(Contrast(&image.frame)<10);
 std::fill(image.bytes.begin(),image.bytes.end(),20);assert(Contrast(&image.frame)<10);
 // Uncorroborated earlier noise must not be counted as several agreeing
 // original bounds: unavailable lanes are not observations.
 image.fill(20,20,0,0,20,66,23,23);for(int x=0;x<64;++x)image.bytes[20*1920+x]=100;
 auto unsupported=image.edges();assert(unsupported.top==Unobserved&&unsupported.rejected);
 // Uniform genuinely thin bars retain the same measurement and common-AR snap.
 image.fill(20,20,0,0,90,90,20,20);auto clean=image.edges();assert(clean.top==20&&clean.bottom==20);
 // Unchanged wider HEVC geometry, including both supported10-bit layouts.
 PixelImage hevc(3840,2160);hevc.fill(262,262,0,0,28,100,846,527);
 auto edge=hevc.edges();assert(edge.top==262&&edge.bottom==262);
 std::vector<Result> wide(7,edge);assert(sampleSelect(wide,3840,2160,result)&&result.top==263&&result.bottom==263);
 std::vector<uint16_t> ten(hevc.bytes.size());
 for(size_t i=0;i<ten.size();++i)ten[i]=static_cast<uint16_t>(hevc.bytes[i]<<2);
 hevc.frame.data[0]=reinterpret_cast<uint8_t*>(ten.data());hevc.frame.linesize[0]=3840*2;hevc.frame.format=AV_PIX_FMT_YUV420P10LE;
 edge=hevc.edges();assert(edge.top==262&&edge.bottom==262);
 for(auto& value:ten)value<<=6;hevc.frame.format=AV_PIX_FMT_P010LE;
 edge=hevc.edges();assert(edge.top==262&&edge.bottom==262);
 // Transposed dark/nonuniform content exercises both independent pillar edges.
 PixelImage pillar;pillar.fill(0,0,240,240,28,100,0,0);
 for(int y=508;y<572;++y)for(int x=240;x<1680;++x)pillar.bytes[y*1920+x]=(x<500||x>=1400)?24:100;
 edge=pillar.edges();assert(edge.left==240&&edge.right==240&&edge.top==Unobserved&&edge.bottom==Unobserved);
 std::vector<Result> side(7,edge);assert(sampleSelect(side,1920,1080,result)&&result.left==240&&result.right==240);
 pillar.fill(0,0,300,300,28,100,0,0);for(int y=508;y<572;++y)for(int x=300;x<1620;++x)pillar.bytes[y*1920+x]=(x<500||x>=1400)?24:100;
 side[6]=pillar.edges();assert(!sampleSelect(side,1920,1080,result));
 // Exact logged arrays must STILL reject; these contain no new pixel evidence.
 uint16_t t[]={20,20,20,123,200,20,152},b[]={20,20,20,223,20,195,20},l[]={0,0,339,207,912,685,312},r[]={433,0,594,598,0,60,381};
 assert(!Select(t,b,l,r,7,1920,1080,result));
 std::cout<<"PASS: controlled1920 thin/noisy bars, dark shoulders, spatial captions, mixed/full frame, pillarbox and3840 10-bit edges through production consensus/snap\n";
 return accepted;
}
void darkEvidence(){
 PixelImage image;
 image.fill(20,20,0,0,28,66,123,223);
 // Textured/vignetted active picture, dark centre shoulders and a near-black
 // object that crosses a distributed strip inside the genuine image area.
 for(int y=20;y<1060;++y)for(int x=0;x<1920;++x)
  if(x<928||x>=992)image.bytes[y*1920+x]=static_cast<uint8_t>(24+(x/11+y/7)%45);
 for(int y=35;y<72;++y)for(int x=0;x<180;++x)image.bytes[y*1920+x]=static_cast<uint8_t>(16+(x+y)%3);
 auto dark=image.edges();assert(dark.rejected&&dark.top==Unobserved);
#ifdef TYPED_EDGE_EVIDENCE
 assert(edgeMeasurements[0].original==123&&edgeMeasurements[0].reason==DetectEdgeReason::RETURN_TO_BLACK);
 assert(edgeMeasurements[0].lane==0&&edgeMeasurements[0].depth==35&&edgeMeasurements[0].departures[0]==20);
 assert(edgeMeasurements[0].returnLevel<=22&&edgeMeasurements[0].borders[0]==16);
 assert(detect_edge_lanes(edgeMeasurements[0]).find("16:20")!=std::string::npos);
 assert(std::string(detect_edge_reason(edgeMeasurements[0].reason))=="return-to-black");
 std::vector<Result> uncertain(7,dark);DetectResult result{};assert(!sampleSelect(uncertain,1920,1080,result));
 // Rejected horizontal spatial evidence also remains a veto; silently dropping
 // such positions could conceal actual captions/mixed framing.
 bool rejected[7]={false,false,false,false,false,false,true};
 uint16_t clean[]={20,20,20,20,20,20,20},zero[7]={};assert(!Select(clean,clean,zero,zero,7,1920,1080,result,rejected));
 // The same textured dark scene can yield reliable pairs when its local edge
 // evidence is adequate. Deep dark objects past the measured prefix do not
 // invalidate a boundary; the guard is bounded by the original estimate.
 image.fill(20,20,0,0,28,66,20,20);
 for(int y=20;y<1060;++y)for(int x=0;x<1920;++x)
  if(x<928||x>=992)image.bytes[y*1920+x]=static_cast<uint8_t>(24+(x/11+y/7)%45);
 for(int y=250;y<400;++y)for(int x=0;x<700;++x)image.bytes[y*1920+x]=16;
 auto reliable=image.edges();assert(!reliable.rejected&&reliable.top==20&&reliable.bottom==20);
 std::vector<Result> supported(7,reliable);assert(sampleSelect(supported,1920,1080,result)&&result.top==21&&result.bottom==21);
 // Unknowns do not manufacture independent or paired votes. Preserve original
 // seven-position denominator and known framing vetoes before abstaining.
 uint16_t t[]={20,20,20,UINT16_MAX,UINT16_MAX,20,UINT16_MAX};
 uint16_t b[]={20,20,20,UINT16_MAX,20,UINT16_MAX,20};
 auto evidence=detect_pair_evidence(t,b,7);assert(evidence.candidate==20&&evidence.first==4&&evidence.second==5&&evidence.paired==3);
 assert(evidence.unknownFirst==3&&evidence.unknownSecond==2&&evidence.zeroFirst==0&&evidence.zeroSecond==0);
 uint16_t border=0;assert(detect_axis_consensus(t,b,7,border)==DetectAxisConfidence::UNCERTAIN&&border==0);
 t[3]=b[3]=0;evidence=detect_pair_evidence(t,b,7);assert(evidence.paired==3&&evidence.zeroFirst==1&&evidence.zeroSecond==1);
 uint16_t known[]={20,20,20,20,20,0,UINT16_MAX},side[]={240,240,240,240,240,240,240};
 assert(!Select(known,known,side,side,7,1920,1080,result));known[5]=40;assert(!Select(known,known,side,side,7,1920,1080,result));
 known[5]=0;uint16_t partial[]={20,20,20,20,20,UINT16_MAX,UINT16_MAX};
 assert(!Select(known,partial,side,side,7,1920,1080,result));
 assert(!Select(partial,known,side,side,7,1920,1080,result));
 known[5]=20;assert(detect_axis_consensus(known,known,7,border)==DetectAxisConfidence::UNCERTAIN);
 // An entirely unobserved/unused orthogonal axis does not prohibit reliable
 // letterbox geometry, nor does it contribute synthetic full-frame votes.
 uint16_t unknown[]={UINT16_MAX,UINT16_MAX,UINT16_MAX,UINT16_MAX,UINT16_MAX,UINT16_MAX,UINT16_MAX};
 assert(Select(clean,clean,unknown,unknown,7,1920,1080,result)&&result.top==21&&result.left==0);
 auto luma=[](int,int)->uint32_t{return 16;};
 auto absent=detect_refine_edge(luma,1920,1080,true,false,0,false);
 auto unused=detect_refine_edge(luma,1920,1080,false,false,0,false,false);
 auto measured=detect_refine_edge(luma,1920,1080,true,false,0,true);
 assert(absent.value==UINT16_MAX&&absent.reason==DetectEdgeReason::NO_TRANSITION&&!absent.RejectedSpatialEvidence());
 assert(unused.value==UINT16_MAX&&unused.reason==DetectEdgeReason::AXIS_UNUSED&&!unused.RejectedSpatialEvidence());
 assert(measured.value==0&&measured.reason==DetectEdgeReason::ORIGINAL&&!measured.RejectedSpatialEvidence());
#endif
 std::cout<<"PASS: realistic dark picture may trigger return-to-black; uncertainty is separate from measured0, remains in coverage, cannot manufacture paired support or hide known framing vetoes\n";
}
void outerEvidence(){
 // A known bright outer strip must not be hidden by an earlier lane's
 // uncertain inner stripe. Keep the zero as smaller-bound veto evidence.
 for(int brightLane=1;brightLane<9;++brightLane){
  PixelImage image;image.fill(40,40,0,0,90,90,40,40);
  for(int x=0;x<64;++x)image.bytes[20*1920+x]=24;
  int start=brightLane*(1920-64)/8;
  for(int x=start;x<start+64;++x)image.bytes[x]=200;
  auto ordered=image.edges();assert(ordered.top==0&&!ordered.rejected);
  std::vector<Result> scans(7,Result{20,20,0,0,false});scans[6]=ordered;DetectResult result{};
  assert(!sampleSelect(scans,1920,1080,result));
 }
 std::cout<<"PASS: all8 later outer lanes establish known zero before inward uncertainty; known smaller framing still vetoes\n";
}
void barGraphicControls(){
 PixelImage image;DetectResult result{};
 // The rejected majority profile hid wider framing as a unilateral outlier.
 for(int flash:{19,20}){
  image.fill(40,40,0,0,90,90,40,40);
  for(int depth=20;depth<40;++depth)for(int x=0;x<1920;++x)
   if(x<900||x>=1020)image.bytes[(1079-depth)*1920+x]=90;
  for(int x=928;x<992;++x)image.bytes[(1079-flash)*1920+x]=24;
  auto edge=image.edges();assert(edge.top==40&&edge.bottom==Unobserved&&edge.rejected);
  std::vector<Result> scans(7,Result{20,20,0,0,false});scans[6]=edge;
  assert(!sampleSelect(scans,1920,1080,result));
 }
 // A narrower return-to-black threshold misreads slightly raised black in
 // the rest of a bar as picture. Keep the observed dim-return veto intact.
 for(int level:{16,18,20,22}){
  image.fill(40,40,0,0,90,90,40,40);
  for(int depth=20;depth<40;++depth)for(int x=0;x<1920;++x){
   int value=depth<28?24:level;image.bytes[depth*1920+x]=value;image.bytes[(1079-depth)*1920+x]=value;
  }
  auto edge=image.edges();assert(edge.top==Unobserved&&edge.bottom==Unobserved&&edge.rejected);
  std::vector<Result> scans(7,Result{20,20,0,0,false});scans[6]=edge;
  assert(!sampleSelect(scans,1920,1080,result));
 }
 std::cout<<"PASS:2 centre-flash and4 raised-black stripe counterexamples retain whole-scan rejection\n";
}
#ifdef NATIVE_NINE_FRAMES
Result readNativeFrame(const char* path){
 std::ifstream in(path,std::ios::binary);std::string magic;int width,height,maxval;
 in>>magic>>width>>height>>maxval;in.get();assert(magic=="P5"&&width==1920&&height==1080&&maxval==255);
 PixelImage image;in.read(reinterpret_cast<char*>(image.bytes.data()),image.bytes.size());
 assert(in.gcount()==static_cast<long>(image.bytes.size()));return image.edges();
}
DetectResult nativeFrames(){
 auto rejected=readNativeFrame(nativeFramePaths[0]);
 assert(rejected.top==Unobserved&&rejected.bottom==Unobserved&&rejected.rejected);
 assert(edgeMeasurements[0].reason==DetectEdgeReason::RETURN_TO_BLACK&&edgeMeasurements[1].reason==DetectEdgeReason::RETURN_TO_BLACK);
 assert(edgeMeasurements[0].original==123&&edgeMeasurements[0].lane==6&&edgeMeasurements[0].depth==92);
 assert(edgeMeasurements[1].original==223&&edgeMeasurements[1].lane==4&&edgeMeasurements[1].depth==38);
 auto corrected=readNativeFrame(nativeFramePaths[1]);
 assert(corrected.top==20&&corrected.bottom==20&&corrected.left==0&&corrected.right==0&&!corrected.rejected);
 assert(edgeMeasurements[3].original==594&&edgeMeasurements[3].reason==DetectEdgeReason::BRIGHT_OUTER&&edgeMeasurements[3].lane==1);
 uint16_t t[]={20,20},b[]={20,20},z[]={0,0};DetectResult result{};
 assert(!Select(t,b,z,z,2,1920,1080,result));
 // HYBRID, not seven movie observations:6 varied generated frames+real30%.
 PixelImage image;std::vector<Result> hybrid;
 for(int i=0;i<6;++i){image.fill(20,20,0,0,26+i%3,50+i*3,123+i*9,223+i*7);hybrid.push_back(image.edges());}
 hybrid.push_back(corrected);assert(sampleSelect(hybrid,1920,1080,result)&&result.top==21&&result.bottom==21);
 auto accepted=result;
 hybrid[5]=rejected;assert(!sampleSelect(hybrid,1920,1080,result));
 image.fill(40,40,0,0);hybrid[5]=image.edges();assert(!sampleSelect(hybrid,1920,1080,result));
 image.fill(0,0,0,0,80,90,0,0);
 for(int x=928;x<992;++x){image.bytes[x]=120;image.bytes[1079*1920+x]=120;}
 hybrid[5]=image.edges();assert(hybrid[5].top==0&&hybrid[5].bottom==0&&!hybrid[5].rejected);
 assert(!sampleSelect(hybrid,1920,1080,result));
 std::cout<<"PASS: actual30%20/20/0/0; actual45% still rejected;2samples cannot publish; explicit hybrid6synthetic+1native reaches consensus and retains conflicting-frame vetoes\n";
 return accepted;
}
#endif
int main(int argc,char** argv){
 assert(argc==2&&ass_library_version()==LIBASS_VERSION&&LIBASS_VERSION>=0x01704000);
#ifdef NATIVE_NINE_FRAMES
 auto native=nativeFrames();
#endif
 darkEvidence();auto result=pixels();outerEvidence();barGraphicControls();
#ifdef NATIVE_NINE_FRAMES
 result=native;
#endif
 ready();select(false,1920,1080);int before=writes.load();
 CRenderManager manager;manager.m_picture.iWidth=1920;manager.m_picture.iHeight=1080;manager.m_overlays.m_rs={0,0,1920,1080};
 auto c=cue(argv[1],"Tijd om te vertrekken.\\N- Hoezo? gypq");auto st=makeStyle();
 auto initial=draw(manager,c,st,true,true);assert(!s_detectSource->result);
 CAMLNativeWorker::Run run(s_detectSource->superseded);detect_publish(run,s_detectSource,result.top,result.bottom,result.left,result.right);
 uint16_t t,b,l,r;assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r)&&t==21&&b==21&&l==0&&r==0);
 assert(writes==before);auto corrected=draw(manager,c,st);rect(manager.m_overlays.m_activePicture,21,1059);
 inside(corrected.bounds,manager.m_overlays.m_activePicture);assert(corrected.bounds.bottom<=1059);
 assert(initial.bounds.bottom>=corrected.bounds.bottom);auto paused=draw(manager,c,st);assert(paused.canvas==corrected.canvas);
 s_detectWorker.Stop();std::cout<<"PASS: extracted thin-bar result -> source publication/getter -> real CE libass -> production atlas/vertices -> final alpha bounds21..1059; no non-DV sysfs writes\n";
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ass-include', type=Path, required=True)
    parser.add_argument('--ass-library', required=True)
    parser.add_argument('--font', type=Path, default=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
    parser.add_argument('--baseline', help='Original estimator; must fail after successful strict compilation')
    parser.add_argument('--native-nine-frames', type=Path, help='Optional directory containing the two hash-pinned original PGMs; no full-film acceptance claim')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    # The surrounding renderer stays current even in the pixel-baseline replay.
    revision = args.baseline
    args.baseline = None
    native_frames = None
    if args.native_nine_frames:
        fixtures = [
            ('nine-45pct-pts2144309.pgm', '23c90045e9a2665b2bfa71a4f1c8d14952d6c29ae80bfb1fbe4447b811ba222a'),
            ('nine-30pct-pts1430095.pgm', '033c80b04558d53a4d021165c19bd6489db56a5665abe99fd5c3c1c7ba288c1b'),
        ]
        native_frames = []
        for name, digest in fixtures:
            path = args.native_nine_frames / name
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
            native_frames.append(path.resolve())
    else:
        print('Native9 fixtures not supplied; only generated pixels exercised.', flush=True)
    code = generate(revision, native_frames)
    result = TEXT['run'](code, args, negative=bool(revision))
    if revision:
        expected = 'corrected.top==20&&corrected.bottom==20&&corrected.left==0&&corrected.right==0&&!corrected.rejected' if native_frames else 'ordered.top==0&&!ordered.rejected'
        assert expected in result.stderr, result.stderr
        print('REJECTED parent outer-evidence ordering after strict compilation:', revision)
    elif args.negative_controls:
        for label, old, new in [
            ('later bright outer evidence hidden', 'const uint32_t border = average(lane, 0);', 'const uint32_t border = lane == 0 ? average(lane, 0) : 16;'),
            ('centre-only edges', '  if (original == 0)', '  return result;\n  if (original == 0)'),
            ('bright centre still sets threshold', 'level > border + 6', 'level > 50 + 0 * border'),
            ('earlier isolated content ignored', 'result.value = support >= 3 ? earliest : UINT16_MAX;\n  result.reason = support >= 3 ? DetectEdgeReason::REFINED : DetectEdgeReason::STRIPS_UNSUPPORTED;', 'result.value = support >= 3 ? earliest : original;\n  result.reason = support >= 3 ? DetectEdgeReason::REFINED : DetectEdgeReason::ORIGINAL;'),
            ('unobserved lanes counted', 'std::fill_n(departures, 9, UINT16_MAX);', 'std::fill_n(departures, 9, edge);'),
            ('broad transient intrusion treated as picture', 'else if (result.departures[lane] != UINT16_MAX)', 'else if (false && result.departures[lane] != UINT16_MAX)'),
            ('sustained side graphic treated as picture', 'if (result.departures[lanes / 2] > earliest + tolerance)', 'if (false && result.departures[lanes / 2] > earliest + tolerance)'),
            ('bilateral mixed aspect veto removed', 'first[i] > candidate + tolerance && second[i] > candidate + tolerance)', 'false && first[i] > candidate + tolerance && second[i] > candidate + tolerance)'),
            ('unknown masks known framing veto', 'int bestSupport = 0;', 'if (unknown) return DetectAxisConfidence::UNCERTAIN; int bestSupport = 0;'),
            ('rejected positions silently used', '[](bool rejected) { return rejected; }', '[](bool rejected) { (void)rejected; return false; }'),
        ]:
            assert old in code, label
            TEXT['run'](code.replace(old, new, 1), args, negative=True)
            print('REJECTED compiled pixel control:', label)


if __name__ == '__main__':
    main()
