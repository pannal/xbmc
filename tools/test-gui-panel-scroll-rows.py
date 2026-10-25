#!/usr/bin/env python3
"""Host regression for panel rows during one-row scroll steps.

Compiles the production CGUIPanelContainer::Process, BuildRenderItems, GetRows
and CorrectOffset with their row helpers from xbmc/guilib/GUIPanelContainer.cpp,
CGUIBaseContainer's ScrollToOffset, UpdateScrollOffset, FreeMemory and offset
range, the CScroller methods and the real cubic tween, against fake container
members, and simulates a six-column poster wall (445-unit rows, 1080-unit
viewport, preload 2) frame by frame. Building an item layout delays the next
frame, as it does on the render thread.

A one-row step down and up from rest must each build exactly one row, in the
frame the step is requested and before its scroll clock starts; nothing may be
built or freed while the scroll moves, and the first moving frame may not jump.
Held scrolling must not gain an extra deferral at retargets and must keep a bounded number of
layouts. Rendered items must be the processed items, also before the first
Process. Rows at rest must equal the historical rows. The historical rule, run
on the same base-class code, must show the down step's per-frame free/rebuild
of its last row's layouts, and destructive mutations of the helpers and of
their production call sites must be rejected. Host fixtures only: no Kodi
rendering, skin, GPU or device timing is executed.
"""
import argparse
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess

PANEL = 'xbmc/guilib/GUIPanelContainer.cpp'
BASE = 'xbmc/guilib/GUIBaseContainer.cpp'
SCROLLER = 'xbmc/guilib/VisibleEffect.cpp'

PROCESS = 'void CGUIPanelContainer::Process('
RENDER = 'void CGUIPanelContainer::BuildRenderItems('


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


def member(source, signature):
    # A base-class member compiled as a member of the fake panel class.
    return function(source, signature).replace('CGUIBaseContainer::', 'CGUIPanelContainer::', 1)


def parts(panel, base, scroller):
    gap = re.search(r'#define SCROLLING_GAP\s+(\S+)', base)
    assert gap, 'SCROLLING_GAP not found'
    process = function(panel, PROCESS)
    assert re.search(r'if \(startNextFrame\)\s*MarkDirtyRegion\(\);\s*else\s*UpdateScrollOffset',
                     process), 'deferred start must skip only the scroller update'
    update = member(base, 'void CGUIBaseContainer::UpdateScrollOffset(').replace(
        'CGUIPanelContainer::UpdateScrollOffset(', 'CGUIPanelContainer::ProductionUpdateScrollOffset(', 1)
    offsets = function(base, 'void CGUIBaseContainer::GetCacheOffsets(').replace(
        'void CGUIBaseContainer::GetCacheOffsets(int &cacheBefore, int &cacheAfter) const',
        'void GetCacheOffsets(const CScroller& m_scroller, int m_cacheItems, int &cacheBefore, int &cacheAfter)')
    methods = [function(scroller, signature) for signature in (
        'CScroller::CScroller(unsigned int duration', 'void CScroller::ScrollTo(float endPos)',
        'bool CScroller::Update(unsigned int time)', 'float CScroller::Tween(float progress)')]
    container = [process, function(panel, RENDER),
                 function(panel, 'unsigned int CGUIPanelContainer::GetRows('),
                 function(panel, 'int CGUIPanelContainer::CorrectOffset('),
                 function(panel, 'void CGUIPanelContainer::ValidateOffset('),
                 member(base, 'void CGUIBaseContainer::ScrollToOffset(').replace(
                     'CGUIPanelContainer::ScrollToOffset(', 'CGUIPanelContainer::BaseScrollToOffset(', 1),
                 function(panel, 'void CGUIPanelContainer::ScrollToOffset(').replace(
                     'CGUIBaseContainer::ScrollToOffset(offset);', 'BaseScrollToOffset(offset);'),
                 member(base, 'bool CGUIBaseContainer::GetOffsetRange('),
                 member(base, 'int CGUIBaseContainer::ScrollCorrectionRange('),
                 member(base, 'void CGUIBaseContainer::FreeMemory('), update]
    helpers = [function(panel, 'void GetPanelRows('), function(panel, 'void GetPanelKeptRows(')]
    return gap.group(1), methods, offsets, helpers, container


PRELUDE = r'''
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <utility>
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
constexpr float kRow = 445, kLength = 1080, kDuration = 500, kColumnWidth = 290;
constexpr int kPreload = 2, kPage = 2, kColumns = 6, kRows = 100;
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
  float width = kColumnWidth, height = kRow;
  float Size(ORIENTATION orientation) const { return orientation == VERTICAL ? height : width; }
};
struct CGUIListItem
{
  int id = 0, currentItem = 0;
  bool layout = false;
  bool HasLayouts() const { return layout; }
  void FreeMemory()
  {
    if (layout)
    {
      layout = false;
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

class CGUIPanelContainer : public CGUIControl
{
public:
  struct RENDERITEM
  {
    float posX;
    float posY;
    std::shared_ptr<CGUIListItem> item;
    bool focused;
  };

  CGUIPanelContainer()
  {
    for (int i = 0; i < kRows * kColumns; ++i)
    {
      auto item = std::make_shared<CGUIListItem>();
      item->id = i;
      m_items.push_back(item);
    }
  }

  // production members
  void Process(unsigned int currentTime, CDirtyRegionList& dirtyregions) override;
  void BuildRenderItems(std::vector<RENDERITEM>& renderItems) const;
  unsigned int GetRows() const;
  int CorrectOffset(int offset, int cursor) const;
  void ScrollToOffset(int offset);
  void BaseScrollToOffset(int offset);
  bool GetOffsetRange(int& minOffset, int& maxOffset) const;
  int ScrollCorrectionRange() const;
  void FreeMemory(int keepStart, int keepEnd);
  void ProductionUpdateScrollOffset(unsigned int currentTime);

  // fakes of the members those use
  void UpdateScrollOffset(unsigned int currentTime)
  {
    updated = true;
    ProductionUpdateScrollOffset(currentTime);
  }
  void ProcessItem(float, float, std::shared_ptr<CGUIListItem>& item, bool, unsigned int, CDirtyRegionList&)
  {
    processed.insert(item->id);
    Check(item->currentItem == item->id + 1, "processed item has its current item number");
    if (!item->layout)
    {
      item->layout = true;
      ++built;
    }
  }
  void ValidateOffset();
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
  CGUIListItemLayout m_template;
  CGUIListItemLayout* m_layout = &m_template;
  CGUIListItemLayout* m_focusedLayout = &m_template;
  ORIENTATION m_orientation = VERTICAL;
  float m_posX = 0, m_posY = 0, m_width = kColumns * kColumnWidth, m_height = kLength;
  CPoint m_renderOffset;
  int m_offset = 0, m_cursor = 0, m_cacheItems = kPreload, m_itemsPerPage = kPage, m_itemsPerRow = kColumns;
  bool m_bInvalidated = false, m_bHasFocus = true, m_wasReset = false;
  std::vector<std::shared_ptr<CGUIListItem>> m_items;
  bool m_wasScrolling{false};
  CStopWatch m_lastScrollStartTimer, m_scrollTimer;

  // observations of one frame
  bool updated = false;
  int built = 0;
  std::set<int> processed;
};
'''

MAIN = r'''
static int failures = 0;
static unsigned long checks = 0;
void CGUIPanelContainer::Check(bool value, const std::string& message)
{
  ++checks;
  if (!value)
  {
    std::fprintf(stderr, "FAIL: %s\n", message.c_str());
    ++failures;
  }
}
static void Check(bool value, const std::string& message)
{
  CGUIPanelContainer::Check(value, message);
}

struct Frame
{
  int built = 0, freed = 0, kept = 0, first = 0, last = 0;
  float before = 0, after = 0;
  bool updated = false, scrolling = false;
};

static std::set<int> Drawn(const CGUIPanelContainer& c)
{
  std::vector<CGUIPanelContainer::RENDERITEM> items;
  c.BuildRenderItems(items);
  std::set<int> drawn;
  for (const auto& item : items)
    drawn.insert(item.item->id);
  return drawn;
}

// Independent physical-row oracle: inspect each existing row against two
// scalar inequalities, without calling either production row helper.
static std::set<int> ExpectedItems(const CGUIPanelContainer& c)
{
  std::set<int> expected;
  if (!c.m_layout || !c.m_focusedLayout) return expected;
  const float size = c.m_layout->Size(c.m_orientation);
  if (!(size > 0)) return expected;
  const float value = c.m_scroller.GetValue(), target = c.GetOffset() * size;
  const float low = std::min(value, target), high = std::max(value, target);
  const float length = c.m_orientation == VERTICAL ? c.m_height : c.m_width;
  const float shift = c.m_orientation == VERTICAL ? c.m_renderOffset.y : c.m_renderOffset.x;
  const int cache = c.GetCacheCount() / 2;
  for (int item = 0; item < static_cast<int>(c.m_items.size()); ++item)
  {
    const int row = item / c.m_itemsPerRow;
    if ((row + cache + 1) * size > low &&
        row * size - high + shift < length + cache * size)
      expected.insert(c.m_items[item]->id);
  }
  return expected;
}
static std::pair<int,int> ExpectedBounds(const CGUIPanelContainer& c)
{
  const auto items = ExpectedItems(c);
  return items.empty() ? std::make_pair(0,-1) :
      std::make_pair(*items.begin()/c.m_itemsPerRow,*items.rbegin()/c.m_itemsPerRow);
}

// The historical rule: rows from the scroll value rounded down, with a preload
// that flips to the travel direction.
static void HistoricalRows(const CGUIPanelContainer& c, int& first, int& last)
{
  int cb, ca;
  GetCacheOffsets(c.m_scroller, kPreload, cb, ca);
  const int offset = static_cast<int>(c.m_scroller.GetValue() / kRow);
  first = offset - cb;
  float pos = c.m_renderOffset.y + first * kRow - c.m_scroller.GetValue();
  const float end = kLength + ca * kRow;
  last = first - 1;
  while (pos < end)
  {
    ++last;
    pos += kRow;
  }
}

struct Panel
{
  explicit Panel(bool old) : historical(old) {}
  bool historical;
  CGUIPanelContainer c;
  unsigned now = 1000;

  void Rest(int row)
  {
    c.m_offset = row;
    c.m_scroller.SetValue(row * kRow);
    Settle();
  }
  void Request(int row) { g_now = now; c.ScrollToOffset(row); }
  int Kept() const
  {
    return static_cast<int>(std::count_if(c.m_items.begin(), c.m_items.end(),
                                          [](const auto& item) { return item->layout; }));
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
      GetCacheOffsets(c.m_scroller, kPreload, cb, ca);
      const int offset = static_cast<int>(c.m_scroller.GetValue() / kRow);
      if (static_cast<int>(c.m_items.size()) > kPage + cb + ca)
        c.FreeMemory((offset - cb) * kColumns, (offset + kPage + 1 + ca) * kColumns);
      HistoricalRows(c, f.first, f.last);
      for (int row = f.first; row <= f.last; ++row)
        for (int col = 0; col < kColumns; ++col)
        {
          const int item = row * kColumns + col;
          if (item >= 0 && item < static_cast<int>(c.m_items.size()))
          {
            c.processed.insert(item);
            if (!c.m_items[item]->layout)
            {
              c.m_items[item]->layout = true;
              ++c.built;
            }
          }
        }
    }
    else
    {
      CDirtyRegionList dirty;
      c.Process(now, dirty);
      std::tie(f.first, f.last) = ExpectedBounds(c);
      Check(c.processed == ExpectedItems(c), "processed items match independent row oracle");
      Check(Drawn(c) == c.processed, "rendered items are the processed items");
    }
    f.updated = c.updated;
    f.built = c.built;
    f.freed = g_freed;
    f.after = c.m_scroller.GetValue();
    f.scrolling = c.m_scroller.IsScrolling();
    f.kept = Kept();
    // visible rows must be processed
    const float value = c.m_scroller.GetValue();
    const int top = static_cast<int>(std::floor(value / kRow));
    for (int row = top; row * kRow - value + c.m_renderOffset.y < kLength; ++row)
      for (int col = 0; col < kColumns; ++col)
      {
        const int item = row * kColumns + col;
        if (row >= 0 && row < kRows)
          Check(c.processed.count(item) == 1, "visible item " + std::to_string(item) + " not processed");
      }
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
  int rowsBuilt = 0, buildFrames = 0, builtAfterMotion = 0, freedWhileScrolling = 0, freedItems = 0;
  int churnFrames = 0; // frames that free and rebuild layouts while moving
  float maxMove = 0;
  bool requestFrameStill = false;
};

static Step RunStep(Panel& panel, int row)
{
  Step step;
  panel.Request(row);
  bool moved = false;
  for (int i = 0; i < 80; ++i)
  {
    const Frame f = panel.Process();
    if (f.built)
    {
      ++step.buildFrames;
      step.rowsBuilt += f.built / kColumns;
      if (moved || f.after != f.before)
        ++step.builtAfterMotion;
      if (i == 0)
        step.requestFrameStill = f.after == f.before;
    }
    if (f.freed && f.scrolling)
      step.freedWhileScrolling += f.freed;
    if (f.freed && f.built && f.scrolling && f.after != f.before)
      ++step.churnFrames;
    step.freedItems += f.freed;
    step.maxMove = std::max(step.maxMove, std::fabs(f.after - f.before));
    moved = moved || f.after != f.before;
    if (!f.scrolling && i > 2)
      break;
  }
  panel.Settle(3);
  return step;
}

static float IdealFirstMove()
{
  const float t = static_cast<float>(kFrame) / kDuration;
  return kRow * (1 - (1 - t) * (1 - t) * (1 - t));
}

int main(int argc, char** argv)
{
  const bool historical = argc > 1 && std::string(argv[1]) == "historical";
  if (historical)
  {
    Panel down{true};
    down.Rest(10);
    RunStep(down, 11); // reach steady stepping
    const Step d = RunStep(down, 12);
    Panel up{true};
    up.Rest(40);
    RunStep(up, 39);
    const Step u = RunStep(up, 38);
    std::printf("historical down: built while moving in %d frames, rebuild churn in %d frames; "
                "up: built while moving in %d frames, rebuild churn in %d frames\n",
                d.builtAfterMotion, d.churnFrames, u.builtAfterMotion, u.churnFrames);
    // witnesses of the reported asymmetry: a step down keeps freeing and
    // rebuilding the layouts of its last row while the posters move
    return (!failures && d.churnFrames >= 5 && d.churnFrames > u.churnFrames && d.builtAfterMotion > 0)
               ? 3
               : 0;
  }

  // one-row steps in both directions, steady state and from a fresh rest
  for (int direction : {+1, -1})
  {
    Panel panel{false};
    const int start = direction > 0 ? 10 : 60;
    panel.Rest(start);
    for (int n = 1; n <= 8; ++n)
    {
      const Step s = RunStep(panel, start + direction * n);
      const std::string name = std::string(direction > 0 ? "down" : "up") + " step " + std::to_string(n);
      Check(s.rowsBuilt == 1, name + ": builds exactly one row (" + std::to_string(s.rowsBuilt) + ")");
      Check(s.buildFrames == 1 && s.requestFrameStill,
            name + ": the row is built in the request frame, before motion");
      Check(s.builtAfterMotion == 0, name + ": nothing built while moving");
      Check(s.freedWhileScrolling == 0, name + ": nothing freed while moving");
      // as before, the kept range ends with the first item of the next row
      Check(s.freedItems >= kColumns - 1 && s.freedItems <= kColumns + 1,
            name + ": one row freed at rest (" + std::to_string(s.freedItems) + " items)");
      Check(s.maxMove <= IdealFirstMove() + 0.01f,
            name + ": no frame jumps further than one undelayed first frame (" +
                std::to_string(s.maxMove) + " > " + std::to_string(IdealFirstMove()) + ")");
    }
  }

  // held scrolling: retargets every 6 frames continue without a pause, bounded memory
  {
    Panel panel{false};
    panel.Rest(5);
    int row = 5, maxKept = 0, unbounded = 0;
    bool firstFrame = true;
    for (int i = 0; i < 180; ++i)
    {
      if (i % 6 == 0 && row < 80)
        panel.Request(++row);
      const Frame f = panel.Process();
      if (!firstFrame && !f.updated)
        Check(false, "held scroll paused at a retarget, frame " + std::to_string(i));
      firstFrame = false;
      maxKept = std::max(maxKept, f.kept);
      if (f.kept > (f.last - f.first + 1 + 2 * kPage + 1) * kColumns)
        ++unbounded;
    }
    Check(unbounded == 0, "held scroll keeps a bounded number of layouts (max " + std::to_string(maxKept) + ")");
    panel.Settle(40);
    const auto [first, last] = ExpectedBounds(panel.c);
    Check(panel.Kept() <= (last - first + 1) * kColumns + kColumns, "rest after held scroll keeps the drawn rows");
  }

  // before the first Process, Render computes the rows Process will use
  for (int direction : {+1, -1})
  {
    CGUIPanelContainer c;
    c.m_offset = 30;
    c.m_scroller.SetValue(30 * kRow);
    c.ScrollToOffset(30 + direction);
    const std::set<int> drawn = Drawn(c);
    CDirtyRegionList dirty;
    c.Process(g_now, dirty);
    Check(drawn == c.processed, "uncached render rows equal the processed rows");
  }

  // rows at rest equal the historical rows
  for (float offset : {0.0f, 12.5f, -20.0f})
    for (int k = 0; k < 60; ++k)
    {
      Panel fixed{false};
      fixed.c.m_renderOffset.y = offset;
      fixed.Rest(k);
      int first, last;
      HistoricalRows(fixed.c, first, last);
      std::set<int> expected;
      for (int i = 0; i < static_cast<int>(fixed.c.m_items.size()); ++i)
        if (i / kColumns >= first && i / kColumns <= last) expected.insert(i);
      Check(Drawn(fixed.c) == expected, "rest rows differ at row " + std::to_string(k));
    }

  // a zero-height layout yields no rows instead of an endless row search
  {
    int first = 7, last = 7;
    GetPanelRows(100, 100, 0, kLength, 0, 1, first, last);
    Check(first > last, "zero row size yields no rows");
  }


  // Geometry/coverage independent of the shared production helper, both axes,
  // zero/odd preload, exact edges, reverse targets and unprocessed retargets.
  for (ORIENTATION axis : {VERTICAL, HORIZONTAL})
    for (int preload : {0, 1, 2, 3, 7})
      for (float shift : {-1200.0f, -20.0f, 0.0f, 100.0f, 1500.0f, 3000.0f})
        for (int target : {0, 9, 10, 11, 70})
        {
          CGUIPanelContainer c;
          c.m_orientation = axis;
          c.m_cacheItems = preload;
          c.m_renderOffset = {shift,shift};
          c.m_offset = 10;
          const float size = c.m_layout->Size(axis);
          c.m_scroller.SetValue(10 * size);
          CDirtyRegionList dirty;
          c.Process(1000,dirty);
          c.ScrollToOffset(target); // prior Process has already populated any cache
          Check(Drawn(c) == ExpectedItems(c), "fresh target before Process");
          // Resize/reposition, then depth/render may run before another Process.
          c.m_width = 2000;c.m_height = 2000;
          Check(Drawn(c) == ExpectedItems(c), "fresh geometry before Process");
          // List replacement keeps current target geometry and takes new owners.
          c.m_items.resize(19);
          Check(Drawn(c) == ExpectedItems(c), "fresh provider size before Process");
          c.processed.clear();c.Process(1100,dirty);
          Check(c.processed == ExpectedItems(c), "edge Process matches independent oracle");
        }

  // Stop between Process calls, then request from rest: row build precedes clock.
  {
    Panel panel{false};panel.Rest(10);panel.Request(11);
    panel.Process();panel.Process();panel.Process();
    panel.c.m_scroller.Stop();panel.Request(40);
    const Frame first=panel.Process();
    Check(!first.updated && first.before==first.after && first.built>0,
          "stopped scroller request defers row-building frame");
  }
  // CScroller stores configured zero as1ms: preserve its existing first anchor,
  // second completion. No extra panel frame delay for zero-duration skins.
  for (unsigned duration : {0u,1u})
  {
    Panel panel{false};panel.c.m_scroller=CScroller(duration);panel.Rest(10);panel.Request(11);
    const Frame first=panel.Process(), second=panel.Process();
    Check(first.updated && first.after==first.before,
          "zero duration anchors in request frame");
    Check(!second.scrolling && second.after==11*kRow,
          "zero duration completes on next frame");
  }
  // Retarget/reversal preserve CScroller's own first-Update zero-progress rule;
  // neither is an additional panel deferral. Repeated requests before first
  // Process retain the one pending build frame.
  {
    Panel panel{false};panel.Rest(20);panel.Request(21);panel.Request(22);
    Check(!panel.Process().updated,"batched initial requests retain build frame");
    panel.Process();panel.Process();
    for (int target : {21,19,24,18})
    {
      panel.Request(target);const Frame frame=panel.Process();
      Check(frame.updated,"active retarget has no extra panel deferral");
      Check(frame.before==frame.after,"retarget preserves inherited zero-progress anchor");
      panel.Process();
    }
  }
  // Long jumps are corrected near their target by the actual base method.
  {
    Panel panel{false};panel.Rest(5);panel.Request(90);
    const Frame frame=panel.Process();
    Check(std::fabs(panel.c.GetOffset()*kRow-frame.after)<=kRow*panel.c.ScrollCorrectionRange(),
          "long jump correction bounds target span");
    Check(frame.kept <= (2*kPage+2*kPreload+5)*kColumns,
          "long jump keeps bounded layout count");
  }
  // Invalid row size is rejected before Process's division/conversion.
  {
    CGUIPanelContainer c;c.m_template.height=0;
    CDirtyRegionList dirty;c.Process(1000,dirty);
    Check(c.processed.empty() && Drawn(c).empty(),"zero-size Process and draw are empty");
    Check(!c.updated,"zero-size Process leaves scroll clock alone");
  }

  if (failures)
    return 1;
  std::printf("CHECKS %lu\n",checks);
  std::printf("PASS: one row per step both ways, built before motion, no build/free while moving, "
              "no first-step jump, no added retarget deferral and bounded held scroll, render rows equal processed rows, "
              "rest rows unchanged\n");
  return 0;
}
'''


def build(out, name, panel, base, scroller):
    gap, methods, offsets, helpers, container = parts(panel, base, scroller)
    source = '\n'.join([PRELUDE.replace('@GAP@', gap), *methods, offsets, *helpers, FAKES,
                        *container, MAIN])
    path = out / (name + '.cpp')
    path.write_text(source)
    exe = out / name
    compiled = subprocess.run(['g++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                               '-Wno-unused-parameter', '-fsanitize=address,undefined',
                               '-fno-sanitize-recover=undefined', '-fno-pie', '-no-pie', '-I', str(ROOT / 'xbmc'),
                               str(path), '-o', str(exe)], capture_output=True, text=True, timeout=120)
    (out / (name + '.compile.log')).write_text(compiled.stdout+compiled.stderr)
    if compiled.returncode:
        raise RuntimeError(name + ' did not compile:\n' + compiled.stderr)
    return exe


def run(exe, *args):
    result = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=60,
                            env={**os.environ, 'ASAN_OPTIONS':'detect_leaks=0',
                                 'UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'})
    suffix = '-' + '-'.join(args) if args else ''
    exe.with_name(exe.name+suffix+'.stdout').write_text(result.stdout)
    exe.with_name(exe.name+suffix+'.stderr').write_text(result.stderr)
    return result


# name: (function the mutation is confined to, or None for the whole file, pattern, replacement)
MUTATIONS = {
    # rows follow only the current scroll value: a step down builds mid-scroll
    'value-only-rows': (None, r'const float high = std::max\(value, target\);', 'const float high = value;'),
    # rows leave and are freed while the scroll moves
    'free-while-scrolling': (None, r'const int margin = scrolling \? rowsPerPage : 0;', 'const int margin = 0;'),
    # the scroll clock starts in the frame that builds the rows
    'immediate-start': (PROCESS, r'const bool startNextFrame =\s*[^;]+;', 'const bool startNextFrame = false;'),
    # every retarget of a running scroll pauses for a frame
    'pause-retargets': (PROCESS, r'const bool startNextFrame =\s*[^;]+;',
                        'const bool startNextFrame = m_scroller.IsScrolling();'),
    # held scrolling keeps every layout it passed
    'unbounded-held': (None, r'const int margin = scrolling \? rowsPerPage : 0;',
                       'const int margin = scrolling ? 1000 : 0;'),
    # Process asks for rows at the scroll value only, not the target
    'process-target-is-value': (PROCESS, r'GetPanelRows\(m_scroller\.GetValue\(\), GetOffset\(\) \* rowSize,',
                                'GetPanelRows(m_scroller.GetValue(), m_scroller.GetValue(),'),
    # Process drops the preload
    'process-preload-ignored': (PROCESS, r'\bcache, firstRow,', '0, firstRow,'),
    # Process frees at rest margins while the scroll moves
    'process-kept-ignores-scrolling': (PROCESS, r'GetPanelKeptRows\(firstRow, lastRow, m_scroller\.IsScrolling\(\),',
                                       'GetPanelKeptRows(firstRow, lastRow, false,'),
    # Process skips the first row it selected
    'process-skips-first-row': (PROCESS, r'for \(int row = firstRow;', 'for (int row = firstRow + 1;'),
    # Render's fallback asks for rows at the scroll value only
    'render-target-is-value': (RENDER, r'GetPanelRows\(m_scroller\.GetValue\(\), GetOffset\(\) \* rowSize,',
                               'GetPanelRows(m_scroller.GetValue(), m_scroller.GetValue(),'),
    'process-zero-size-guard': (PROCESS, r'if \(!\(rowSize > 0\)\)\s*return;', '(void)0;'),
    'zero-duration-delay': (PROCESS, r' && m_scroller.GetDuration\(\) > 1', ''),
    # the reset lives in the shared container ScrollToOffset that the panel calls
    'stale-stopped-state': ('void CGUIBaseContainer::ScrollToOffset(',
                            r'if \(!m_scroller.IsScrolling\(\)\)\s*m_wasScrolling = false;', '(void)0;'),
    'empty-edge-row': (None, r'lastRow = firstRow - 1;',
                       'lastRow = static_cast<int>(std::floor(high / rowSize)) + cache;'),

}

ROOT = Path(__file__).resolve().parents[1]


def mutate(panel, scope, pattern, replacement):
    target = function(panel, scope) if scope else panel
    mutated, count = re.subn(pattern, replacement, target)
    return panel.replace(target, mutated) if scope else mutated, count


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=Path('/tmp/gui-panel-scroll-rows'))
    args = parser.parse_args()
    ROOT = args.root
    args.output.mkdir(parents=True, exist_ok=True)
    panel, base, scroller = [(ROOT / path).read_text() for path in (PANEL, BASE, SCROLLER)]

    exe = build(args.output, 'fixed', panel, base, scroller)
    historical = run(exe, 'historical')
    assert historical.returncode == 3, (historical.returncode, historical.stdout, historical.stderr)
    print('Historical rule reproduced: ' + historical.stdout.strip(), flush=True)
    fixed = run(exe)
    assert fixed.returncode == 0, (fixed.stdout, fixed.stderr)
    print(fixed.stdout.strip(), flush=True)

    for name, (scope, pattern, replacement) in MUTATIONS.items():
        inBase = bool(scope) and scope.startswith('void CGUIBaseContainer::')
        mutated, count = mutate(base if inBase else panel, scope, pattern, replacement)
        assert count == 1, 'mutation did not apply: ' + name
        bad = run(build(args.output, 'mutant-' + name, panel if inBase else mutated,
                        mutated if inBase else base, scroller))
        witnesses = {
            'value-only-rows':'nothing built while moving', 'free-while-scrolling':'nothing freed while moving',
            'immediate-start':'no frame jumps further than one undelayed first frame',
            'pause-retargets':'held scroll paused', 'unbounded-held':'bounded',
            'process-target-is-value':'processed items match independent row oracle', 'process-preload-ignored':'independent row oracle',
            'process-kept-ignores-scrolling':'nothing freed while moving',
            'process-skips-first-row':'rendered items are the processed items',
            'render-target-is-value':'uncached render rows equal',
            'process-zero-size-guard':'zero-size Process leaves scroll clock alone',
            'zero-duration-delay':'zero duration anchors in request frame',
            'stale-stopped-state':'stopped scroller request defers',
            'empty-edge-row':'fresh target before Process',
        }
        assert (bad.returncode == 1 and 'FAIL: ' in bad.stderr and witnesses[name] in bad.stderr
                and not any(x in bad.stderr for x in ('AddressSanitizer', 'UndefinedBehaviorSanitizer',
                                                       'runtime error:', 'LeakSanitizer'))), (
                    'Missing semantic witness: '+name,bad.returncode,bad.stdout,bad.stderr)
        print('Rejected mutation: ' + name, flush=True)
    (args.output / 'provenance.json').write_text(json.dumps({
        'source_sha256':{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest()
                         for path in (PANEL,BASE,SCROLLER,'xbmc/guilib/GUIPanelContainer.h')},
        'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'historical_witness':historical.stdout.strip(),
        'current':fixed.stdout.strip(), 'semantic_mutants':list(MUTATIONS),
        'limits':__doc__, 'sanitizers':'ASan/UBSan, LSan disabled; nonPIE; no device'},indent=2)+'\n')
    print('GUI panel scroll rows: PASS; historical asymmetry reproduced, %d mutations rejected; '
          'host fixtures, not skin/GPU/device verification' % len(MUTATIONS))


if __name__ == '__main__':
    main()
