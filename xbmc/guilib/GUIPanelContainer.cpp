/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "GUIPanelContainer.h"

#include "FileItem.h"
#include "GUIListItemLayout.h"
#include "GUIMessage.h"
#include "guilib/guiinfo/GUIInfoLabels.h"
#include "input/actions/Action.h"
#include "input/actions/ActionIDs.h"
#include "utils/StringUtils.h"

#include <algorithm>
#include <cassert>
#include <cmath>

namespace
{
// Rows processed and drawn: the viewport at the current scroll value and at the
// scroll target, plus the same preload on both sides. A step's new row enters
// in the frame it is requested, before its scroll starts, whatever the
// direction; during the scroll rows only leave.
void GetPanelRows(float value,
                  float target,
                  float rowSize,
                  float length,
                  float renderOffset,
                  int cache,
                  int& firstRow,
                  int& lastRow)
{
  if (!(rowSize > 0))
  { // no rows to lay out
    firstRow = 0;
    lastRow = -1;
    return;
  }
  const float low = std::min(value, target);
  const float high = std::max(value, target);
  firstRow = static_cast<int>(std::floor(low / rowSize)) - cache;
  // Include only rows starting before the far edge, even when a positive
  // render offset moves the entire viewport before the first candidate row.
  lastRow = firstRow - 1;
  const float end = length + cache * rowSize;
  while ((lastRow + 1) * rowSize - high + renderOffset < end)
    ++lastRow;
}

// Rows whose layouts are kept. At rest the drawn rows; while scrolling one more
// page each side, so a step frees nothing mid-scroll and a held scroll stays
// bounded.
void GetPanelKeptRows(
    int firstRow, int lastRow, bool scrolling, int rowsPerPage, int& keepFirst, int& keepLast)
{
  const int margin = scrolling ? rowsPerPage : 0;
  keepFirst = firstRow - margin;
  keepLast = lastRow + margin;
}
} // namespace

CGUIPanelContainer::CGUIPanelContainer(int parentID, int controlID, float posX, float posY, float width, float height, ORIENTATION orientation, const CScroller& scroller, int preloadItems)
    : CGUIBaseContainer(parentID, controlID, posX, posY, width, height, orientation, scroller, preloadItems)
{
  ControlType = GUICONTAINER_PANEL;
  m_type = VIEW_TYPE_ICON;
  m_itemsPerRow = 1;
}

CGUIPanelContainer::~CGUIPanelContainer(void) = default;

void CGUIPanelContainer::Process(unsigned int currentTime, CDirtyRegionList &dirtyregions)
{
  ValidateOffset();

  if (m_bInvalidated)
    UpdateLayout();

  if (!m_layout || !m_focusedLayout)
    return;

  const float rowSize = m_layout->Size(m_orientation);
  if (!(rowSize > 0))
    return;

  // A scroll requested from rest starts its clock on the next frame: this frame
  // builds the rows it reveals, so their build time is not shown as a jump in
  // its first step. Retargets keep the existing scroller update policy.
  // A configured zero duration is represented by one millisecond. Keep its
  // existing clock update rather than adding an otherwise visible frame delay.
  const bool startNextFrame =
      m_scroller.IsScrolling() && !m_wasScrolling && m_scroller.GetDuration() > 1;
  if (startNextFrame)
    MarkDirtyRegion();
  else
    UpdateScrollOffset(currentTime);
  m_wasScrolling = m_scroller.IsScrolling();

  const int offset = static_cast<int>(m_scroller.GetValue() / rowSize);
  const int cache = GetCacheCount() / 2;
  int firstRow, lastRow;
  GetPanelRows(m_scroller.GetValue(), GetOffset() * rowSize, rowSize,
               (m_orientation == VERTICAL) ? m_height : m_width,
               (m_orientation == VERTICAL) ? m_renderOffset.y : m_renderOffset.x, cache, firstRow,
               lastRow);

  // Free memory not used on screen
  if ((int)m_items.size() > m_itemsPerPage + 2 * cache)
  {
    int keepFirst, keepLast;
    GetPanelKeptRows(firstRow, lastRow, m_scroller.IsScrolling(), m_itemsPerPage, keepFirst,
                     keepLast);
    FreeMemory(CorrectOffset(keepFirst, 0), CorrectOffset(keepLast + 1, 0));
  }

  CPoint origin = CPoint(m_posX, m_posY) + m_renderOffset;
  float pos = (m_orientation == VERTICAL) ? origin.y : origin.x;
  pos += firstRow * rowSize - m_scroller.GetValue();

  const int itemCount = static_cast<int>(m_items.size());
  for (int row = firstRow; row <= lastRow && row * m_itemsPerRow < itemCount; ++row, pos += rowSize)
  {
    for (int col = 0; col < m_itemsPerRow; ++col)
    {
      const int current = row * m_itemsPerRow + col;
      if (current < 0)
        continue;
      if (current >= itemCount)
        break;
      std::shared_ptr<CGUIListItem> item = m_items[current];
      item->SetCurrentItem(current + 1);
      bool focused = (current == GetOffset() * m_itemsPerRow + GetCursor()) && m_bHasFocus;

      if (m_orientation == VERTICAL)
        ProcessItem(origin.x + col * m_layout->Size(HORIZONTAL), pos, item, focused, currentTime, dirtyregions);
      else
        ProcessItem(pos, origin.y + col * m_layout->Size(VERTICAL), item, focused, currentTime, dirtyregions);
    }
  }

  // when we are scrolling up, offset will become lower (integer division, see offset calc)
  // to have same behaviour when scrolling down, we need to set page control to offset+1
  UpdatePageControl(offset + (m_scroller.IsScrollingDown() ? 1 : 0));

  CGUIControl::Process(currentTime, dirtyregions);
}


void CGUIPanelContainer::BuildRenderItems(std::vector<RENDERITEM>& renderItems) const
{
  renderItems.clear();
  if (!m_layout || !m_focusedLayout)
    return;

  // Re-evaluate current geometry for both depth assignment and rendering. A
  // jump, provider replacement or layout change can precede the next Process().
  const float rowSize = m_layout->Size(m_orientation);
  int firstRow, lastRow;
  GetPanelRows(m_scroller.GetValue(), GetOffset() * rowSize, rowSize,
               (m_orientation == VERTICAL) ? m_height : m_width,
               (m_orientation == VERTICAL) ? m_renderOffset.y : m_renderOffset.x,
               GetCacheCount() / 2, firstRow, lastRow);

  CPoint origin = CPoint(m_posX, m_posY) + m_renderOffset;
  float pos = (m_orientation == VERTICAL) ? origin.y : origin.x;
  pos += firstRow * rowSize - m_scroller.GetValue();

  float focusedPos = 0;
  int focusedCol = 0;
  std::shared_ptr<CGUIListItem> focusedItem;
  const int itemCount = static_cast<int>(m_items.size());
  for (int row = firstRow; row <= lastRow && row * m_itemsPerRow < itemCount; ++row, pos += rowSize)
  {
    for (int col = 0; col < m_itemsPerRow; ++col)
    {
      const int current = row * m_itemsPerRow + col;
      if (current < 0)
        continue;
      if (current >= itemCount)
        break;
      std::shared_ptr<CGUIListItem> item = m_items[current];
      bool focused = (current == GetOffset() * m_itemsPerRow + GetCursor()) && m_bHasFocus;
      // render our item
      if (focused)
      {
        focusedPos = pos;
        focusedCol = col;
        focusedItem = item;
      }
      else
      {
        if (m_orientation == VERTICAL)
          renderItems.emplace_back(
              RENDERITEM{origin.x + col * m_layout->Size(HORIZONTAL), pos, item, false});
        else
          renderItems.emplace_back(
              RENDERITEM{pos, origin.y + col * m_layout->Size(VERTICAL), item, false});
      }
    }
  }
  // and render the focused item last (for overlapping purposes)
  if (focusedItem)
  {
    if (m_orientation == VERTICAL)
      renderItems.emplace_back(RENDERITEM{origin.x + focusedCol * m_layout->Size(HORIZONTAL),
                                          focusedPos, focusedItem, true});
    else
      renderItems.emplace_back(RENDERITEM{
          focusedPos, origin.y + focusedCol * m_layout->Size(VERTICAL), focusedItem, true});
  }
}

void CGUIPanelContainer::Render()
{
  if (!m_layout || !m_focusedLayout)
    return;

  if (CServiceBroker::GetWinSystem()->GetGfxContext().SetClipRegion(m_posX, m_posY, m_width, m_height))
  {
    std::vector<RENDERITEM> renderitems;
    BuildRenderItems(renderitems);

    if (CServiceBroker::GetWinSystem()->GetGfxContext().GetRenderOrder() ==
        RENDER_ORDER_FRONT_TO_BACK)
    {
      for (auto it = std::crbegin(renderitems); it != std::crend(renderitems); it++)
      {
        RenderItem(it->posX, it->posY, it->item.get(), it->focused);
      }
    }
    else
    {
      for (const auto& renderitem : renderitems)
      {
        RenderItem(renderitem.posX, renderitem.posY, renderitem.item.get(), renderitem.focused);
      }
    }

    CServiceBroker::GetWinSystem()->GetGfxContext().RestoreClipRegion();
  }
  CGUIControl::Render();
}

bool CGUIPanelContainer::OnAction(const CAction &action)
{
  switch (action.GetID())
  {
  case ACTION_PAGE_UP:
    {
      if (GetOffset() == 0)
      { // already on the first page, so move to the first item
        SetCursor(0);
      }
      else
      { // scroll up to the previous page
        Scroll( -m_itemsPerPage);
      }
      return true;
    }
    break;
  case ACTION_PAGE_DOWN:
    {
      if ((GetOffset() + m_itemsPerPage) * m_itemsPerRow >= (int)m_items.size() || (int)m_items.size() < m_itemsPerPage)
      { // already at the last page, so move to the last item.
        SetCursor(m_items.size() - GetOffset() * m_itemsPerRow - 1);
      }
      else
      { // scroll down to the next page
        Scroll(m_itemsPerPage);
      }
      return true;
    }
    break;
    // smooth scrolling (for analog controls)
  case ACTION_SCROLL_UP:
    {
      m_analogScrollCount += action.GetAmount() * action.GetAmount();
      bool handled = false;
      while (m_analogScrollCount > AnalogScrollSpeed())
      {
        handled = true;
        m_analogScrollCount -= AnalogScrollSpeed();
        if (GetOffset() > 0)// && GetCursor() <= m_itemsPerPage * m_itemsPerRow / 2)
        {
          Scroll(-1);
        }
        else if (GetCursor() > 0)
        {
          SetCursor(GetCursor() - 1);
        }
      }
      return handled;
    }
    break;
  case ACTION_SCROLL_DOWN:
    {
      m_analogScrollCount += action.GetAmount() * action.GetAmount();
      bool handled = false;
      while (m_analogScrollCount > AnalogScrollSpeed())
      {
        handled = true;
        m_analogScrollCount -= AnalogScrollSpeed();
        if ((GetOffset() + m_itemsPerPage) * m_itemsPerRow < (int)m_items.size())// && GetCursor() >= m_itemsPerPage * m_itemsPerRow / 2)
        {
          Scroll(1);
        }
        else if (GetCursor() < m_itemsPerPage * m_itemsPerRow - 1 && GetOffset() * m_itemsPerRow + GetCursor() < (int)m_items.size() - 1)
        {
          SetCursor(GetCursor() + 1);
        }
      }
      return handled;
    }
    break;
  }
  return CGUIBaseContainer::OnAction(action);
}

bool CGUIPanelContainer::OnMessage(CGUIMessage& message)
{
  if (message.GetControlId() == GetID() )
  {
    if (message.GetMessage() == GUI_MSG_LABEL_RESET)
    {
      SetCursor(0);
      // fall through to base class
    }
  }
  return CGUIBaseContainer::OnMessage(message);
}

void CGUIPanelContainer::OnLeft()
{
  CGUIAction action = GetAction(ACTION_MOVE_LEFT);
  bool wrapAround = action.GetNavigation() == GetID() || !action.HasActionsMeetingCondition();
  if (m_orientation == VERTICAL && MoveLeft(wrapAround))
    return;
  if (m_orientation == HORIZONTAL && MoveUp(wrapAround))
    return;
  CGUIControl::OnLeft();
}

void CGUIPanelContainer::OnRight()
{
  CGUIAction action = GetAction(ACTION_MOVE_RIGHT);
  bool wrapAround = action.GetNavigation() == GetID() || !action.HasActionsMeetingCondition();
  if (m_orientation == VERTICAL && MoveRight(wrapAround))
    return;
  if (m_orientation == HORIZONTAL && MoveDown(wrapAround))
    return;
  return CGUIControl::OnRight();
}

void CGUIPanelContainer::OnUp()
{
  CGUIAction action = GetAction(ACTION_MOVE_UP);
  bool wrapAround = action.GetNavigation() == GetID() || !action.HasActionsMeetingCondition();
  if (m_orientation == VERTICAL && MoveUp(wrapAround))
    return;
  if (m_orientation == HORIZONTAL && MoveLeft(wrapAround))
    return;
  CGUIControl::OnUp();
}

void CGUIPanelContainer::OnDown()
{
  CGUIAction action = GetAction(ACTION_MOVE_DOWN);
  bool wrapAround = action.GetNavigation() == GetID() || !action.HasActionsMeetingCondition();
  if (m_orientation == VERTICAL && MoveDown(wrapAround))
    return;
  if (m_orientation == HORIZONTAL && MoveRight(wrapAround))
    return;
  return CGUIControl::OnDown();
}

bool CGUIPanelContainer::MoveDown(bool wrapAround)
{
  if (GetCursor() + m_itemsPerRow < m_itemsPerPage * m_itemsPerRow && (GetOffset() + 1 + GetCursor() / m_itemsPerRow) * m_itemsPerRow < (int)m_items.size())
  { // move to last item if necessary
    if ((GetOffset() + 1)*m_itemsPerRow + GetCursor() >= (int)m_items.size())
      SetCursor((int)m_items.size() - 1 - GetOffset()*m_itemsPerRow);
    else
      SetCursor(GetCursor() + m_itemsPerRow);
  }
  else if ((GetOffset() + 1 + GetCursor() / m_itemsPerRow) * m_itemsPerRow < (int)m_items.size())
  { // we scroll to the next row, and move to last item if necessary
    if ((GetOffset() + 1)*m_itemsPerRow + GetCursor() >= (int)m_items.size())
      SetCursor((int)m_items.size() - 1 - (GetOffset() + 1)*m_itemsPerRow);
    ScrollToOffset(GetOffset() + 1);
  }
  else if (wrapAround)
  { // move first item in list
    SetCursor(GetCursor() % m_itemsPerRow);
    ScrollToOffset(0);
    SetContainerMoving(1);
  }
  else
    return false;
  return true;
}

bool CGUIPanelContainer::MoveUp(bool wrapAround)
{
  if (GetCursor() >= m_itemsPerRow)
    SetCursor(GetCursor() - m_itemsPerRow);
  else if (GetOffset() > 0)
    ScrollToOffset(GetOffset() - 1);
  else if (wrapAround)
  { // move last item in list in this column
    SetCursor((GetCursor() % m_itemsPerRow) + (m_itemsPerPage - 1) * m_itemsPerRow);
    int offset = std::max((int)GetRows() - m_itemsPerPage, 0);
    // should check here whether cursor is actually allowed here, and reduce accordingly
    if (offset * m_itemsPerRow + GetCursor() >= (int)m_items.size())
      SetCursor((int)m_items.size() - offset * m_itemsPerRow - 1);
    ScrollToOffset(offset);
    SetContainerMoving(-1);
  }
  else
    return false;
  return true;
}

bool CGUIPanelContainer::MoveLeft(bool wrapAround)
{
  int col = GetCursor() % m_itemsPerRow;
  if (col > 0)
    SetCursor(GetCursor() - 1);
  else if (wrapAround)
  { // wrap around
    SetCursor(GetCursor() + m_itemsPerRow - 1);
    if (GetOffset() * m_itemsPerRow + GetCursor() >= (int)m_items.size())
      SetCursor((int)m_items.size() - GetOffset() * m_itemsPerRow - 1);
  }
  else
    return false;
  return true;
}

bool CGUIPanelContainer::MoveRight(bool wrapAround)
{
  int col = GetCursor() % m_itemsPerRow;
  if (col + 1 < m_itemsPerRow && GetOffset() * m_itemsPerRow + GetCursor() + 1 < (int)m_items.size())
    SetCursor(GetCursor() + 1);
  else if (wrapAround) // move first item in row
    SetCursor(GetCursor() - col);
  else
    return false;
  return true;
}

// scrolls the said amount
void CGUIPanelContainer::Scroll(int amount)
{
  // increase or decrease the offset
  int offset = GetOffset() + amount;
  if (offset > ((int)GetRows() - m_itemsPerPage) * m_itemsPerRow)
  {
    offset = ((int)GetRows() - m_itemsPerPage) * m_itemsPerRow;
  }
  if (offset < 0) offset = 0;
  ScrollToOffset(offset);
}

void CGUIPanelContainer::ValidateOffset()
{
  if (!m_layout) return;
  // first thing is we check the range of our offset
  // don't validate offset if we are scrolling in case the tween image exceed <0, 1> range
  if (GetOffset() > (int)GetRows() - m_itemsPerPage || (!m_scroller.IsScrolling() && m_scroller.GetValue() > ((int)GetRows() - m_itemsPerPage) * m_layout->Size(m_orientation)))
  {
    SetOffset(std::max(0, (int)GetRows() - m_itemsPerPage));
    m_scroller.SetValue(GetOffset() * m_layout->Size(m_orientation));
  }
  if (GetOffset() < 0 || (!m_scroller.IsScrolling() && m_scroller.GetValue() < 0))
  {
    SetOffset(0);
    m_scroller.SetValue(0);
  }
}

void CGUIPanelContainer::SetCursor(int cursor)
{
  // exceeds the number of items the panel can hold
  if (cursor > m_itemsPerPage * m_itemsPerRow - 1)
    cursor = m_itemsPerPage * m_itemsPerRow - 1;

  // exceeds the number of items being displayed
  const int itemsOn = m_items.size() - 1 - GetOffset() * m_itemsPerRow;
  if (cursor > itemsOn)
    cursor = itemsOn;

  if (cursor < 0)
    cursor = 0;

  if (!m_wasReset)
    SetContainerMoving(cursor - GetCursor());
  CGUIBaseContainer::SetCursor(cursor);
}

void CGUIPanelContainer::CalculateLayout()
{
  GetCurrentLayouts();

  if (!m_layout || !m_focusedLayout) return;
  // calculate the number of items to display
  if (m_orientation == HORIZONTAL)
  {
    m_itemsPerRow = (int)(m_height / m_layout->Size(VERTICAL));
    m_itemsPerPage = (int)(m_width / m_layout->Size(HORIZONTAL));
  }
  else
  {
    m_itemsPerRow = (int)(m_width / m_layout->Size(HORIZONTAL));
    m_itemsPerPage = (int)(m_height / m_layout->Size(VERTICAL));
  }
  if (m_itemsPerRow < 1) m_itemsPerRow = 1;
  if (m_itemsPerPage < 1) m_itemsPerPage = 1;

  // ensure that the scroll offset is a multiple of our size
  m_scroller.SetValue(GetOffset() * m_layout->Size(m_orientation));
}

unsigned int CGUIPanelContainer::GetRows() const
{
  assert(m_itemsPerRow > 0);
  return (m_items.size() + m_itemsPerRow - 1) / m_itemsPerRow;
}

float CGUIPanelContainer::AnalogScrollSpeed() const
{
  return 10.0f / m_itemsPerPage;
}

int CGUIPanelContainer::CorrectOffset(int offset, int cursor) const
{
  return offset * m_itemsPerRow + cursor;
}

int CGUIPanelContainer::GetCursorFromPoint(const CPoint &point, CPoint *itemPoint) const
{
  if (!m_layout)
    return -1;

  float sizeX = m_orientation == VERTICAL ? m_layout->Size(HORIZONTAL) : m_layout->Size(VERTICAL);
  float sizeY = m_orientation == VERTICAL ? m_layout->Size(VERTICAL) : m_layout->Size(HORIZONTAL);

  float posY = m_orientation == VERTICAL ? point.y : point.x;
  for (int y = 0; y < m_itemsPerPage + 1; y++) // +1 to ensure if we have a half item we can select it
  {
    float posX = m_orientation == VERTICAL ? point.x : point.y;
    for (int x = 0; x < m_itemsPerRow; x++)
    {
      int item = x + y * m_itemsPerRow;
      if (posX < sizeX && posY < sizeY && item + GetOffset() < (int)m_items.size())
      { // found
        return item;
      }
      posX -= sizeX;
    }
    posY -= sizeY;
  }
  return -1;
}

bool CGUIPanelContainer::SelectItemFromPoint(const CPoint &point)
{
  int cursor = GetCursorFromPoint(point);
  if (cursor < 0)
    return false;
  SetCursor(cursor);
  return true;
}

int CGUIPanelContainer::GetCurrentRow() const
{
  return m_itemsPerRow > 0 ? GetCursor() / m_itemsPerRow : 0;
}

int CGUIPanelContainer::GetCurrentColumn() const
{
  return GetCursor() % m_itemsPerRow;
}

bool CGUIPanelContainer::GetCondition(int condition, int data) const
{
  int row = GetCurrentRow();
  int col = GetCurrentColumn();

  if (m_orientation == HORIZONTAL)
    std::swap(row, col);

  switch (condition)
  {
  case CONTAINER_ROW:
    return (row == data);
  case CONTAINER_COLUMN:
    return (col == data);
  default:
    return CGUIBaseContainer::GetCondition(condition, data);
  }
}

std::string CGUIPanelContainer::GetLabel(int info) const
{
  int row = GetCurrentRow();
  int col = GetCurrentColumn();

  if (m_orientation == HORIZONTAL)
    std::swap(row, col);

  switch (info)
  {
  case CONTAINER_ROW:
    return std::to_string(row);
  case CONTAINER_COLUMN:
    return std::to_string(col);
  default:
    return CGUIBaseContainer::GetLabel(info);
  }
  return StringUtils::Empty;
}

void CGUIPanelContainer::SelectItem(int item)
{
  // Check that our offset is valid
  ValidateOffset();
  // only select an item if it's in a valid range
  if (item >= 0 && item < (int)m_items.size())
  {
    // Select the item requested
    if (item >= GetOffset() * m_itemsPerRow && item < (GetOffset() + m_itemsPerPage) * m_itemsPerRow)
    { // the item is on the current page, so don't change it.
      SetCursor(item - GetOffset() * m_itemsPerRow);
    }
    else if (item < GetOffset() * m_itemsPerRow)
    { // item is on a previous page - make it the first item on the page
      SetCursor(item % m_itemsPerRow);
      ScrollToOffset((item - GetCursor()) / m_itemsPerRow);
    }
    else // (item >= GetOffset()+m_itemsPerPage)
    { // item is on a later page - make it the last row on the page
      SetCursor(item % m_itemsPerRow + m_itemsPerRow * (m_itemsPerPage - 1));
      ScrollToOffset((item - GetCursor()) / m_itemsPerRow);
    }
  }
}

bool CGUIPanelContainer::HasPreviousPage() const
{
  return (GetOffset() > 0);
}

bool CGUIPanelContainer::HasNextPage() const
{
  return (GetOffset() != (int)GetRows() - m_itemsPerPage && (int)GetRows() > m_itemsPerPage);
}

void CGUIPanelContainer::ScrollToOffset(int offset)
{
  // Stop() can run between Process() calls (for example in FreeResources()).
  // A subsequent request from rest still needs its row-building frame.
  if (!m_scroller.IsScrolling())
    m_wasScrolling = false;
  CGUIBaseContainer::ScrollToOffset(offset);
  SetCursor(GetCursor());
}
