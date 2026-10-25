#!/usr/bin/env python3
"""Host regression for list, fixed list and wrap list items during one-item steps.

Compiles the production CGUIBaseContainer::Process, BuildRenderItems,
GetDrawRange, ScrollToOffset, UpdateScrollOffset, FreeMemory, CorrectOffset,
GetOffsetRange, GetRows and ScrollCorrectionRange from
xbmc/guilib/GUIBaseContainer.cpp, the wrap list's CorrectOffset and
GetOffsetRange, the CScroller methods and the real cubic tween against fake
container members, and steps a horizontal poster list like a fixed list with
focus position 1 (317-unit items, a 490-unit focused item, a 2112-unit
viewport, 500 ms cubic scroll) frame by frame. Building an item layout delays
the next frame, as it does on the render thread.

A one-item step left and right from rest, with preload 1 and 2, must build its
new layouts in the frame the step is requested, before its scroll clock
starts; nothing may be built or freed while the scroll moves, and the first
moving frame may not jump. Processed and rendered items must equal an
independent item enumeration in every frame, held scrolling must not gain an
extra deferral at retargets and must keep a bounded number of layouts, and
items at rest must equal the historical items. A wrap list keeps all of a list
its kept range covers. A zero duration keeps its existing first frame, and a
request after a stop between frames still defers. The historical rule, run on
the same base code, must build while moving, and destructive mutations of the
production code must be rejected. Host fixtures only: no Kodi rendering, skin,
GPU or device timing is executed.
"""
import argparse
from pathlib import Path
import os
import re
import subprocess

BASE = 'xbmc/guilib/GUIBaseContainer.cpp'
WRAP = 'xbmc/guilib/GUIWrappingListContainer.cpp'
SCROLLER = 'xbmc/guilib/VisibleEffect.cpp'

PROCESS = 'void CGUIBaseContainer::Process('
RANGE = 'bool CGUIBaseContainer::GetDrawRange('
SCROLL = 'void CGUIBaseContainer::ScrollToOffset('


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


def parts(base, wrap, scroller):
    gap = re.search(r'#define SCROLLING_GAP\s+(\S+)', base)
    assert gap, 'SCROLLING_GAP not found'
    process = function(base, PROCESS)
    assert re.search(r'if \(startNextFrame\)\s*MarkDirtyRegion\(\);\s*else\s*UpdateScrollOffset',
                     process), 'deferred start must skip only the scroller update'
    update = function(base, 'void CGUIBaseContainer::UpdateScrollOffset(').replace(
        'CGUIBaseContainer::UpdateScrollOffset(', 'CGUIBaseContainer::ProductionUpdateScrollOffset(', 1)
    offsets = function(base, 'void CGUIBaseContainer::GetCacheOffsets(').replace(
        'void CGUIBaseContainer::GetCacheOffsets(int &cacheBefore, int &cacheAfter) const',
        'void GetCacheOffsets(const CScroller& m_scroller, int m_cacheItems, int &cacheBefore, int &cacheAfter)')
    methods = [function(scroller, signature) for signature in (
        'CScroller::CScroller(unsigned int duration', 'void CScroller::ScrollTo(float endPos)',
        'bool CScroller::Update(unsigned int time)', 'float CScroller::Tween(float progress)')]
    container = [process, function(base, 'void CGUIBaseContainer::BuildRenderItems('),
                 function(base, RANGE), function(base, SCROLL),
                 function(base, 'bool CGUIBaseContainer::GetOffsetRange('),
                 function(base, 'unsigned int CGUIBaseContainer::GetRows('),
                 function(base, 'int CGUIBaseContainer::ScrollCorrectionRange('),
                 function(base, 'int CGUIBaseContainer::CorrectOffset('),
                 function(base, 'void CGUIBaseContainer::FreeMemory('), update,
                 function(wrap, 'bool CGUIWrappingListContainer::GetOffsetRange('),
                 function(wrap, 'int CGUIWrappingListContainer::CorrectOffset(')]
    return gap.group(1), methods, offsets, container


PRELUDE = r'''
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <set>
#include <string>
#include <vector>
#include "guilib/Tween.h"

#define SCROLLING_GAP @GAP@

class CScroller
{
public:
  CScroller(unsigned int duration = 200, std::shared_ptr<Tweener> tweener = std::shared_ptr<Tweener>());
  void ScrollTo(float endPos);
  bool Update(unsigned int time);
  float GetValue() const { return m_scrollValue; }
  void SetValue(float scrollValue) { m_scrollValue = scrollValue; }
  bool IsScrolling() const { return m_delta != 0; }
  void Stop() { m_delta = 0; }
  unsigned int GetDuration() const { return m_duration; }
  bool IsScrollingUp() const { return m_delta < 0; }
  bool IsScrollingDown() const { return m_delta > 0; }
private:
  float Tween(float progress);
  float m_scrollValue;
  float m_delta;
  float m_startPosition;
  bool m_hasResumePoint;
  unsigned int m_startTime;
  unsigned int m_duration;
  std::shared_ptr<Tweener> m_pTweener;
};
'''

FAKES = r'''
constexpr float kItem = 317, kFocused = 490, kLength = 2112, kDuration = 500, kHeight = 476;
constexpr int kCursor = 1, kItems = 200;
// as CGUIBaseContainer::CalculateLayout: (2112 - 490) / 317 + 1
constexpr int kPage = static_cast<int>((kLength - kFocused) / kItem) + 1;
constexpr unsigned kFrame = 16, kBuildCost = 4; // ms per frame, ms per built layout

static unsigned g_now = 1000;
static int g_freed = 0;

enum ORIENTATION { HORIZONTAL = 0, VERTICAL };
struct CPoint
{
  float x = 0, y = 0;
  CPoint() = default;
  CPoint(float px, float py) : x(px), y(py) {}
  CPoint operator+(const CPoint& other) const { return CPoint(x + other.x, y + other.y); }
};
using CDirtyRegionList = std::vector<int>;
struct CGUIListItemLayout
{
  float width = kItem, height = kHeight;
  float Size(ORIENTATION orientation) const { return orientation == VERTICAL ? height : width; }
};
struct CGUIListItem
{
  int id = 0, currentItem = 0;
  bool normal = false, focused = false;
  bool HasLayouts() const { return normal || focused; }
  void FreeMemory()
  {
    if (HasLayouts())
    {
      normal = focused = false;
      ++g_freed;
    }
  }
  void SetCurrentItem(int item) { currentItem = item; }
};
struct CStopWatch
{
  bool running = false;
  unsigned start = 0;
  bool IsRunning() const { return running; }
  unsigned GetElapsedMilliseconds() const { return g_now - start; }
  void Start() { if (!running) StartZero(); }
  void StartZero() { running = true; start = g_now; }
  void Stop() { running = false; }
};
struct CGUIControl
{
  virtual ~CGUIControl() = default;
  virtual void Process(unsigned int, CDirtyRegionList&) {}
};

class CGUIBaseContainer : public CGUIControl
{
public:
  struct RENDERITEM
  {
    float posX;
    float posY;
    std::shared_ptr<CGUIListItem> item;
    bool focused;
  };

  explicit CGUIBaseContainer(int count = kItems)
  {
    for (int i = 0; i < count; ++i)
    {
      auto item = std::make_shared<CGUIListItem>();
      item->id = i;
      m_items.push_back(item);
    }
    m_focusedTemplate.width = kFocused;
  }

  // production members
  void Process(unsigned int currentTime, CDirtyRegionList& dirtyregions) override;
  void BuildRenderItems(std::vector<RENDERITEM>& renderItems) const;
  bool GetDrawRange(int& firstItem, float& firstPos, float& endPos) const;
  virtual void ScrollToOffset(int offset);
  virtual bool GetOffsetRange(int& minOffset, int& maxOffset) const;
  virtual unsigned int GetRows() const;
  int ScrollCorrectionRange() const;
  virtual int CorrectOffset(int offset, int cursor) const;
  void FreeMemory(int keepStart, int keepEnd);
  void ProductionUpdateScrollOffset(unsigned int currentTime);

  // fakes of the members those use
  void UpdateScrollOffset(unsigned int currentTime)
  {
    updated = true;
    ProductionUpdateScrollOffset(currentTime);
  }
  void ProcessItem(float, float, std::shared_ptr<CGUIListItem>& item, bool focused, unsigned int,
                   CDirtyRegionList&)
  {
    processed.insert(item->id);
    Check(item->currentItem == item->id + 1, "processed item has its current item number");
    bool& layout = focused ? item->focused : item->normal;
    if (!layout)
    {
      layout = true;
      ++built;
    }
  }
  void UpdateAutoScrolling(unsigned int) {}
  void ValidateOffset() {}
  void UpdateLayout() {}
  void MarkDirtyRegion() {}
  void UpdatePageControl(int) {}
  void SetContainerMoving(int) {}
  int GetOffset() const { return m_offset; }
  void SetOffset(int offset) { m_offset = offset; }
  int GetCursor() const { return m_cursor; }
  void SetCursor(int cursor) { m_cursor = cursor; }
  int GetCacheCount() const { return m_cacheItems; }
  static void Check(bool value, const std::string& message);

  CScroller m_scroller{static_cast<unsigned>(kDuration), [] {
    auto tweener = std::make_shared<CubicTweener>();
    tweener->SetEasing(EASE_OUT); // <scrolltime tween="cubic" easing="out">
    return tweener;
  }()};
  CGUIListItemLayout m_template, m_focusedTemplate;
  CGUIListItemLayout* m_layout = &m_template;
  CGUIListItemLayout* m_focusedLayout = &m_focusedTemplate;
  ORIENTATION m_orientation = HORIZONTAL;
  float m_posX = 0, m_posY = 0, m_width = kLength, m_height = kHeight;
  CPoint m_renderOffset;
  int m_offset = 0, m_cursor = kCursor, m_cacheItems = 1, m_itemsPerPage = kPage;
  bool m_bInvalidated = false, m_bHasFocus = true, m_wasReset = false;
  bool m_waitForScrollEnd = false, m_gestureActive = false;
  unsigned int m_lastRenderTime = 0;
  std::vector<std::shared_ptr<CGUIListItem>> m_items;
  bool m_wasScrolling{false};
  CStopWatch m_lastScrollStartTimer, m_scrollTimer;

  // observations of one frame
  bool updated = false;
  int built = 0;
  std::set<int> processed;
};

class CGUIWrappingListContainer : public CGUIBaseContainer
{
public:
  using CGUIBaseContainer::CGUIBaseContainer;
  bool GetOffsetRange(int& minOffset, int& maxOffset) const override;
  int CorrectOffset(int offset, int cursor) const override;
};
'''

MAIN = r'''
static int failures = 0;
void CGUIBaseContainer::Check(bool value, const std::string& message)
{
  if (!value)
  {
    std::fprintf(stderr, "FAIL: %s\n", message.c_str());
    ++failures;
  }
}
static void Check(bool value, const std::string& message)
{
  CGUIBaseContainer::Check(value, message);
}

static std::set<int> Drawn(const CGUIBaseContainer& c)
{
  std::vector<CGUIBaseContainer::RENDERITEM> items;
  c.BuildRenderItems(items);
  std::set<int> drawn;
  for (const auto& item : items)
    drawn.insert(item.item->id);
  return drawn;
}

// Independent item oracle: every index whose position, past a larger focused
// item, starts before the far edge of the viewport at the higher of the scroll
// value and target, from the viewport at the lower, with half the preload on
// each side, without calling the production range helper.
static std::set<int> ExpectedItems(const CGUIBaseContainer& c)
{
  std::set<int> expected;
  const float size = c.m_layout->Size(c.m_orientation);
  if (!(size > 0) || c.m_items.empty())
    return expected;
  const float value = c.m_scroller.GetValue(), target = c.GetOffset() * size;
  const int cache = c.GetCacheCount() / 2, focus = c.GetOffset() + c.GetCursor();
  const float grow = c.m_focusedLayout->Size(c.m_orientation) - size;
  const float edge = c.m_orientation == VERTICAL ? c.m_height : c.m_width;
  const float shift = c.m_orientation == VERTICAL ? c.m_renderOffset.y : c.m_renderOffset.x;
  for (int index = static_cast<int>(std::floor(std::min(value, target) / size)) - cache;; ++index)
  {
    const float pos = shift + index * size - value + (index > focus ? grow : 0);
    if (!(pos < edge + std::max(value, target) - value + cache * size))
      break;
    const int item = c.CorrectOffset(index, 0);
    if (item >= static_cast<int>(c.m_items.size()))
      break;
    if (item >= 0)
      expected.insert(item);
  }
  return expected;
}

// The historical rule: items from the scroll value rounded down, with a preload
// that flips to the travel direction.
static std::set<int> HistoricalItems(const CGUIBaseContainer& c)
{
  std::set<int> items;
  int cb, ca;
  GetCacheOffsets(c.m_scroller, c.m_cacheItems, cb, ca);
  const float value = c.m_scroller.GetValue();
  const int offset = static_cast<int>(std::floor(value / kItem));
  float pos = c.m_renderOffset.x + (offset - cb) * kItem - value;
  if (c.GetOffset() + c.GetCursor() < offset)
    pos += kFocused - kItem;
  const float end = kLength + ca * kItem;
  for (int index = offset - cb; pos < end; ++index)
  {
    const int item = c.CorrectOffset(index, 0);
    if (item >= static_cast<int>(c.m_items.size()))
      break;
    if (item >= 0)
      items.insert(item);
    pos += index == c.GetOffset() + c.GetCursor() ? kFocused : kItem;
  }
  return items;
}

struct Frame
{
  int built = 0, freed = 0, kept = 0;
  float before = 0, after = 0;
  bool updated = false, scrolling = false;
};

template<class C>
struct List
{
  explicit List(bool old, int preload = 1, int count = kItems) : historical(old), c(count)
  {
    c.m_cacheItems = preload;
  }
  bool historical;
  C c;
  unsigned now = 1000;

  void Rest(int offset)
  {
    c.m_offset = offset;
    c.m_scroller.SetValue(offset * kItem);
    Settle();
  }
  void Request(int offset) { g_now = now; c.ScrollToOffset(offset); }
  int Kept() const
  {
    return static_cast<int>(std::count_if(c.m_items.begin(), c.m_items.end(),
                                          [](const auto& item) { return item->HasLayouts(); }));
  }
  Frame Process()
  {
    Frame f;
    g_now = now;
    g_freed = 0;
    c.updated = false;
    c.built = 0;
    c.processed.clear();
    f.before = c.m_scroller.GetValue();
    if (historical)
    {
      c.UpdateScrollOffset(now);
      int cb, ca;
      GetCacheOffsets(c.m_scroller, c.m_cacheItems, cb, ca);
      const int offset = static_cast<int>(std::floor(c.m_scroller.GetValue() / kItem));
      if (static_cast<int>(c.m_items.size()) > kPage + cb + ca)
        c.FreeMemory(c.CorrectOffset(offset - cb, 0), c.CorrectOffset(offset + kPage + 1 + ca, 0));
      for (int item : HistoricalItems(c))
      {
        c.processed.insert(item);
        auto& entry = *c.m_items[item];
        bool& layout = item == c.CorrectOffset(c.GetOffset() + c.GetCursor(), 0) ? entry.focused : entry.normal;
        if (!layout)
        {
          layout = true;
          ++c.built;
        }
      }
    }
    else
    {
      CDirtyRegionList dirty;
      c.Process(now, dirty);
      Check(c.processed == ExpectedItems(c), "processed items match independent item oracle");
      Check(Drawn(c) == c.processed, "rendered items are the processed items");
    }
    f.updated = c.updated;
    f.built = c.built;
    f.freed = g_freed;
    f.after = c.m_scroller.GetValue();
    f.scrolling = c.m_scroller.IsScrolling();
    f.kept = Kept();
    now += kFrame + kBuildCost * f.built;
    return f;
  }
  void Settle(int frames = 5)
  {
    for (int i = 0; i < frames; ++i)
      Process();
  }
};

struct Step
{
  int built = 0, buildFrames = 0, builtAfterMotion = 0, freedWhileScrolling = 0;
  float maxMove = 0;
  bool requestFrameStill = false;
};

template<class L>
static Step RunStep(L& list, int offset)
{
  Step step;
  list.Request(offset);
  bool moved = false;
  for (int i = 0; i < 80; ++i)
  {
    const Frame f = list.Process();
    if (f.built)
    {
      ++step.buildFrames;
      step.built += f.built;
      if (moved || f.after != f.before)
        ++step.builtAfterMotion;
      if (i == 0)
        step.requestFrameStill = f.after == f.before;
    }
    if (f.freed && f.scrolling)
      step.freedWhileScrolling += f.freed;
    step.maxMove = std::max(step.maxMove, std::fabs(f.after - f.before));
    moved = moved || f.after != f.before;
    if (!f.scrolling && i > 2)
      break;
  }
  list.Settle(3);
  return step;
}

static float IdealFirstMove()
{
  const float t = static_cast<float>(kFrame) / kDuration;
  return kItem * (1 - (1 - t) * (1 - t) * (1 - t));
}

int main(int argc, char** argv)
{
  const bool historical = argc > 1 && std::string(argv[1]) == "historical";
  if (historical)
  {
    int moving = 0, jumps = 0;
    for (int direction : {+1, -1})
    {
      List<CGUIBaseContainer> list{true};
      list.Rest(50);
      for (int n = 1; n <= 4; ++n)
      {
        const Step s = RunStep(list, 50 + direction * n);
        moving += s.builtAfterMotion;
        jumps += s.maxMove > IdealFirstMove() + 0.01f;
      }
    }
    std::printf("historical: %d frames build while moving, %d steps jump in their first frame\n", moving,
                jumps);
    return (!failures && moving + jumps >= 4) ? 3 : 0;
  }

  // one-item steps both ways, preload 1 and 2, list and wrap list
  for (int preload : {1, 2})
    for (int direction : {+1, -1})
    {
      List<CGUIBaseContainer> list{false, preload};
      List<CGUIWrappingListContainer> wrap{false, preload};
      list.Rest(50);
      wrap.Rest(50);
      for (int n = 1; n <= 8; ++n)
        for (int kind = 0; kind < 2; ++kind)
        {
          const Step s = kind ? RunStep(wrap, 50 + direction * n) : RunStep(list, 50 + direction * n);
          const std::string name = std::string(kind ? "wrap " : "list ") + (direction > 0 ? "right" : "left") +
                                   " step " + std::to_string(n) + " preload " + std::to_string(preload);
          Check(s.built >= 1 && s.buildFrames == 1 && s.requestFrameStill,
                name + ": its layouts are built in the request frame, before motion");
          Check(s.builtAfterMotion == 0, name + ": nothing built while moving");
          Check(s.freedWhileScrolling == 0, name + ": nothing freed while moving");
          Check(s.maxMove <= IdealFirstMove() + 0.01f,
                name + ": no frame jumps further than one undelayed first frame (" +
                    std::to_string(s.maxMove) + " > " + std::to_string(IdealFirstMove()) + ")");
        }
    }

  // held scrolling: retargets every 6 frames continue without a pause, bounded memory
  {
    List<CGUIBaseContainer> list{false};
    list.Rest(20);
    int offset = 20, maxKept = 0;
    bool firstFrame = true;
    for (int i = 0; i < 180; ++i)
    {
      if (i % 6 == 0 && offset < 120)
        list.Request(++offset);
      const Frame f = list.Process();
      if (!firstFrame && !f.updated)
        Check(false, "held scroll paused at a retarget, frame " + std::to_string(i));
      firstFrame = false;
      maxKept = std::max(maxKept, f.kept);
    }
    Check(maxKept <= 4 * kPage + 6, "held scroll keeps a bounded number of layouts (max " +
                                        std::to_string(maxKept) + ")");
    list.Settle(40);
    Check(list.Kept() <= static_cast<int>(ExpectedItems(list.c).size()) + 1,
          "rest after held scroll keeps the drawn items");
  }

  // items at rest equal the historical items
  for (int preload : {0, 1, 2})
    for (float shift : {0.0f, 40.5f, -100.0f})
      for (int offset : {0, 3, 50, kItems - kPage})
      {
        List<CGUIBaseContainer> list{false, preload};
        list.c.m_renderOffset.x = shift;
        list.Rest(offset);
        Check(Drawn(list.c) == HistoricalItems(list.c),
              "rest items differ at offset " + std::to_string(offset));
      }

  // a wrap list the kept range covers keeps all of its layouts while stepping
  {
    List<CGUIWrappingListContainer> wrap{false, 2, kPage + 3};
    wrap.Rest(0);
    int freed = 0;
    for (int n = 1; n <= 12; ++n)
    {
      wrap.Request(n);
      for (int i = 0; i < 40; ++i)
        freed += wrap.Process().freed;
    }
    Check(freed == 0, "a short wrap list keeps its layouts (" + std::to_string(freed) + " freed)");
  }

  // before the first Process, Render computes the items Process will use
  for (int direction : {+1, -1})
  {
    CGUIBaseContainer c;
    c.m_offset = 30;
    c.m_scroller.SetValue(30 * kItem);
    c.ScrollToOffset(30 + direction);
    const std::set<int> drawn = Drawn(c);
    CDirtyRegionList dirty;
    c.Process(g_now, dirty);
    Check(drawn == c.processed, "uncached render items equal the processed items");
  }

  // Stop between Process calls, then request from rest: the item build precedes the clock.
  {
    List<CGUIBaseContainer> list{false};
    list.Rest(10);
    list.Request(11);
    list.Process();
    list.Process();
    list.c.m_scroller.Stop();
    list.Request(40);
    const Frame first = list.Process();
    Check(!first.updated && first.before == first.after && first.built > 0,
          "stopped scroller request defers item-building frame");
  }

  // CScroller stores a configured zero as 1 ms: keep its existing first frame
  for (unsigned duration : {0u, 1u})
  {
    List<CGUIBaseContainer> list{false};
    list.c.m_scroller = CScroller(duration);
    list.Rest(10);
    list.Request(11);
    const Frame first = list.Process(), second = list.Process();
    Check(first.updated && first.after == first.before, "zero duration anchors in request frame");
    Check(!second.scrolling && second.after == 11 * kItem, "zero duration completes on next frame");
  }

  // an invalid item size is rejected before Process's division
  {
    CGUIBaseContainer c;
    c.m_template.width = 0;
    CDirtyRegionList dirty;
    c.Process(1000, dirty);
    Check(c.processed.empty() && Drawn(c).empty(), "zero-size Process and draw are empty");
    Check(!c.updated, "zero-size Process leaves scroll clock alone");
  }

  if (failures)
    return 1;
  std::printf("PASS: one-item steps both ways build before motion, no build/free while moving, "
              "no first-step jump, items match the oracle, unpaused bounded held scroll, rest items "
              "unchanged, short wrap lists kept\n");
  return 0;
}
'''


def build(out, name, base, wrap, scroller):
    gap, methods, offsets, container = parts(base, wrap, scroller)
    source = '\n'.join([PRELUDE.replace('@GAP@', gap), *methods, offsets, FAKES, *container, MAIN])
    path = out / (name + '.cpp')
    path.write_text(source)
    exe = out / name
    compiled = subprocess.run(['g++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                               '-Wno-unused-parameter', '-fsanitize=address,undefined',
                               '-fno-sanitize-recover=undefined', '-I', str(ROOT / 'xbmc'),
                               str(path), '-o', str(exe)], capture_output=True, text=True, timeout=120)
    if compiled.returncode:
        raise RuntimeError(name + ' did not compile:\n' + compiled.stderr)
    return exe


def run(exe, *args):
    return subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=120,
                          env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})


# name: (function the mutation is confined to, pattern, replacement, required failure)
MUTATIONS = {
    'value-only-items': (RANGE, r'const float target = GetOffset\(\) \* size;', 'const float target = value;',
                         'nothing built while moving'),
    'preload-ignored': (RANGE, r'const int cache = GetCacheCount\(\) / 2;', 'const int cache = 0;',
                        'rest items differ'),
    'no-far-preload': (RANGE, r'std::max\(value, target\) - value \+ cache \* size;',
                       'std::max(value, target) - value;', 'independent item oracle'),
    'immediate-start': (PROCESS, r'const bool startNextFrame =\s*[^;]+;', 'const bool startNextFrame = false;',
                        'no frame jumps further'),
    'pause-retargets': (PROCESS, r'const bool startNextFrame =\s*[^;]+;',
                        'const bool startNextFrame = m_scroller.IsScrolling();', 'held scroll paused'),
    'free-while-scrolling': (PROCESS, r'const int margin = m_scroller\.IsScrolling\(\) \? m_itemsPerPage : 0;',
                             'const int margin = 0;', 'nothing freed while moving'),
    'unbounded-held': (PROCESS, r'const int margin = m_scroller\.IsScrolling\(\) \? m_itemsPerPage : 0;',
                       'const int margin = m_scroller.IsScrolling() ? 1000 : 0;', 'bounded number of layouts'),
    'wrap-overlap': (PROCESS, r'if \(keepLast - keepFirst <= \(int\)m_items\.size\(\)\)', 'if (true)',
                     'short wrap list keeps its layouts'),
    # without the guard the item index overflows: the sanitizer is the witness
    'zero-size-guard': (PROCESS, r'if \(!\(size > 0\)\)\s*return;', '(void)0;', 'runtime error:'),
    'zero-duration-delay': (PROCESS, r' && m_scroller\.GetDuration\(\) > 1', '',
                            'zero duration anchors in request frame'),
    'stale-stopped-state': (SCROLL, r'if \(!m_scroller\.IsScrolling\(\)\)\s*m_wasScrolling = false;', '(void)0;',
                            'stopped scroller request defers'),
    'skip-first-item': (PROCESS, r'GetDrawRange\(current, pos, end\);', 'GetDrawRange(current, pos, end);\n  ++current;',
                        'independent item oracle'),
}

ROOT = Path(__file__).resolve().parents[1]


def mutate(source, scope, pattern, replacement):
    target = function(source, scope)
    mutated, count = re.subn(pattern, replacement, target)
    return source.replace(target, mutated), count


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=Path('/tmp/gui-list-scroll-items'))
    args = parser.parse_args()
    ROOT = args.root
    args.output.mkdir(parents=True, exist_ok=True)
    base, wrap, scroller = [(ROOT / path).read_text() for path in (BASE, WRAP, SCROLLER)]

    exe = build(args.output, 'fixed', base, wrap, scroller)
    historical = run(exe, 'historical')
    assert historical.returncode == 3, (historical.returncode, historical.stdout, historical.stderr)
    print('Historical rule reproduced: ' + historical.stdout.strip(), flush=True)
    fixed = run(exe)
    assert fixed.returncode == 0, (fixed.stdout, fixed.stderr)
    print(fixed.stdout.strip(), flush=True)

    for name, (scope, pattern, replacement, witness) in MUTATIONS.items():
        mutated, count = mutate(base, scope, pattern, replacement)
        assert count == 1, 'mutation did not apply: ' + name
        bad = run(build(args.output, 'mutant-' + name, mutated, wrap, scroller))
        sanitized = any(x in bad.stderr for x in ('AddressSanitizer', 'runtime error:'))
        assert (bad.returncode == 1 and witness in bad.stderr and
                (witness == 'runtime error:' or not sanitized)), (
                    'destructive mutation survived or failed for another reason: ' + name, bad.returncode,
                    bad.stderr[:2000])
        print('Rejected mutation: ' + name, flush=True)
    print('GUI list scroll items: PASS; historical rule reproduced, %d mutations rejected; '
          'host fixtures, not skin/GPU/device verification' % len(MUTATIONS))


if __name__ == '__main__':
    main()
