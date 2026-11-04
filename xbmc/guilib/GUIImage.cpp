/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "GUIImage.h"

#include "GUIComponent.h"
#include "GUILargeTextureManager.h"
#include "GUIMessage.h"
#include "ServiceBroker.h"
#include "settings/AdvancedSettings.h"
#include "settings/SettingsComponent.h"
#include "utils/log.h"

#include <algorithm>
#include <cassert>

using namespace KODI::GUILIB;

CGUIImage::CGUIImage(int parentID,
                     int controlID,
                     float posX,
                     float posY,
                     float width,
                     float height,
                     const CTextureInfo& texture)
  : CGUIControl(parentID, controlID, posX, posY, width, height),
    m_texture(CGUITexture::CreateTexture(posX, posY, width, height, texture))
{
  m_crossFadeTime = 0;
  m_currentFadeTime = 0;
  m_lastRenderTime = 0;
  ControlType = GUICONTROL_IMAGE;
  m_bDynamicResourceAlloc=false;
}

CGUIImage::CGUIImage(const CGUIImage& left)
  : CGUIControl(left),
    m_image(left.m_image),
    m_info(left.m_info),
    m_texture(left.m_texture->Clone()),
    m_fadingTextures(),
    m_currentTexture(),
    m_currentFallback(),
    m_lastDiffuseColor(left.m_lastDiffuseColor)
{
  m_crossFadeTime = left.m_crossFadeTime;
  // defaults
  m_currentFadeTime = 0;
  m_lastRenderTime = 0;
  ControlType = GUICONTROL_IMAGE;
  m_bDynamicResourceAlloc=false;
}

CGUIImage::~CGUIImage(void) = default;

void CGUIImage::UpdateVisibility(const CGUIListItem *item)
{
  CGUIControl::UpdateVisibility(item);

  // now that we've checked for conditional info, we can
  // check for allocation
  AllocateOnDemand();
}

void CGUIImage::UpdateDiffuseColor(const CGUIListItem* item)
{
  if (m_texture->SetDiffuseColor(m_diffuseColor, item))
  {
    MarkDirtyRegion();
  }
}

void CGUIImage::UpdateInfo(const CGUIListItem *item)
{
  // The texture may also depend on info conditions. Update the diffuse color in that case.
  if (m_texture->GetDiffuseColor().HasInfo())
    UpdateDiffuseColor(item);

  if (m_info.IsConstant())
    return; // nothing to do

  // don't allow image to change while animating out
  if (HasProcessed() && IsAnimating(ANIM_TYPE_HIDDEN) && !IsVisibleFromSkin())
    return;

  // A list row shows its own item. A window image follows the window's
  // selection only if its texture uses list item info, not playback art.
  m_texture->SetFollowsSelection(!item && m_info.IsListItemDependent());
  if (item)
    SetFileName(m_info.GetItemLabel(item, true, &m_currentFallback));
  else
    SetFileName(m_info.GetLabel(m_parentID, true, &m_currentFallback));
}

void CGUIImage::AllocateOnDemand()
{
  // if we're hidden, we can free our resources and return
  if (!IsVisible() && m_visible != DELAYED)
  {
    if (m_bDynamicResourceAlloc && m_texture->IsAllocated())
      FreeResourcesButNotAnims();
    return;
  }

  // either visible or delayed - we need the resources allocated in either case
  if (!m_texture->IsAllocated())
    AllocResources();
}

void CGUIImage::Process(unsigned int currentTime, CDirtyRegionList &dirtyregions)
{
  // check whether our image failed to allocate, and if so drop back to the fallback image
  if (m_texture->FailedToAlloc() && m_texture->GetFileName() != m_info.GetFallback())
  {
    if (!m_currentFallback.empty() && m_texture->GetFileName() != m_currentFallback)
      m_texture->SetFileName(m_currentFallback);
    else
      m_texture->SetFileName(m_info.GetFallback());
  }

  if (m_crossFadeTime)
  {
    bool changed = false;

    // make sure our texture has started allocating
    changed |= m_texture->AllocResources();

    // compute the frame time
    unsigned int frameTime = 0;
    if (m_lastRenderTime)
      frameTime = currentTime - m_lastRenderTime;
    if (!frameTime)
      frameTime = (unsigned int)(1000 / CServiceBroker::GetWinSystem()->GetGfxContext().GetFPS());
    m_lastRenderTime = currentTime;

    // A crossfade draws a background image twice, see-through. While the GUI
    // moves and background images wait for motion, do not run it: show the
    // incoming image once it is ready, else keep the last outgoing image that
    // shows anything at full strength, also when there is no incoming image.
    bool holdOutgoing = false;
    if ((!m_fadingTextures.empty() || m_currentFadeTime != m_crossFadeTime) &&
        m_texture->IsBackgroundRequest() &&
        CServiceBroker::GetGUI()->GetLargeTextureManager().IsBackgroundMotion())
    {
      const bool ready = m_texture->ReadyToRender();
      CFadingTexture* kept = nullptr;
      if (!ready)
      {
        const auto visible = std::find_if(m_fadingTextures.rbegin(), m_fadingTextures.rend(),
                                          [](const CFadingTexture* texture)
                                          { return !texture->m_texture->GetFileName().empty(); });
        if (visible != m_fadingTextures.rend())
          kept = *visible;
      }
      for (CFadingTexture* texture : m_fadingTextures)
      {
        if (texture != kept)
        {
          delete texture;
          changed = true;
        }
      }
      m_fadingTextures.clear();
      if (kept)
      {
        kept->m_fadeTime = m_crossFadeTime;
        kept->m_dissolve = true; // alone at full strength again
        m_fadingTextures.push_back(kept);
        holdOutgoing = true;
      }
      if (ready)
        m_currentFadeTime = m_crossFadeTime;
    }

    if (m_fadingTextures.size())  // have some fading images
    { // anything other than the last old texture needs to be faded out as per usual
      for (std::vector<CFadingTexture *>::iterator i = m_fadingTextures.begin(); i != m_fadingTextures.end() - 1;)
      {
        if (!ProcessFading(*i, frameTime, currentTime, changed))
          i = m_fadingTextures.erase(i);
        else
          ++i;
      }

      if (!holdOutgoing && (m_texture->ReadyToRender() || m_texture->GetFileName().empty()))
      { // fade out the last one as well
        if (!ProcessFading(m_fadingTextures[m_fadingTextures.size() - 1], frameTime, currentTime, changed))
          m_fadingTextures.erase(m_fadingTextures.end() - 1);
      }
      else
      { // keep the last one fading in
        CFadingTexture *texture = m_fadingTextures[m_fadingTextures.size() - 1];
        texture->m_fadeTime += frameTime;
        if (texture->m_fadeTime > m_crossFadeTime)
          texture->m_fadeTime = m_crossFadeTime;

        changed |= texture->m_texture->SetAlpha(GetFadeLevel(texture->m_fadeTime));
        changed |= texture->m_texture->SetDiffuseColor(m_diffuseColor);
        changed |= texture->m_texture->Process(currentTime);
      }
    }

    if (m_texture->ReadyToRender() || m_texture->GetFileName().empty())
    { // fade the new one in
      m_currentFadeTime += frameTime;
      if (m_currentFadeTime > m_crossFadeTime || frameTime == 0) // for if we allocate straight away on creation
        m_currentFadeTime = m_crossFadeTime;
    }
    changed |= m_texture->SetAlpha(GetFadeLevel(m_currentFadeTime));

    if (changed)
      MarkDirtyRegion();
  }

  if (!m_texture->GetDiffuseColor().HasInfo())
  {
    // Only update diffuse color if it has changed
    UTILS::COLOR::Color currentColor = m_diffuseColor;
    if (currentColor != m_lastDiffuseColor)
    {
      m_lastDiffuseColor = currentColor;
      UpdateDiffuseColor(nullptr);
    }
  }

  if (m_texture->Process(currentTime))
    MarkDirtyRegion();

  if (m_pendingBackground && (m_fadingTextures.empty() || !m_crossFadeTime))
  {
    auto request = std::move(*m_pendingBackground);
    m_pendingBackground.reset();
    SetFileName(request.first, false, request.second);
  }

  CGUIControl::Process(currentTime, dirtyregions);
}

void CGUIImage::Render()
{
  if (!IsVisible()) return;

  // Draw a crossfade pair in one pass, shading each pixel once instead of once
  // per image. The result equals drawing the outgoing and then the incoming
  // image; an opaque pair keeps the outgoing image opaque and dissolves.
  if (m_fadingTextures.size() == 1 &&
      CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->m_guiSinglePassCrossfade &&
      m_texture->ReadyToRender() && m_fadingTextures.front()->m_texture->ReadyToRender() &&
      m_texture->RenderCrossfade(*m_fadingTextures.front()->m_texture,
                                 m_fadingTextures.front()->m_dissolve))
  {
    CGUIControl::Render();
    return;
  }

  for (auto& itr : m_fadingTextures)
    itr->m_texture->Render();

  m_texture->Render();

  CGUIControl::Render();
}

bool CGUIImage::ProcessFading(CGUIImage::CFadingTexture *texture, unsigned int frameTime, unsigned int currentTime, bool &changed)
{
  assert(texture);
  if (texture->m_fadeTime <= frameTime)
  { // time to kill off the texture
    changed = true;
    delete texture;
    return false;
  }
  // render this texture
  texture->m_fadeTime -= frameTime;

  changed |= texture->m_texture->SetAlpha(GetFadeLevel(texture->m_fadeTime));
  changed |= texture->m_texture->SetDiffuseColor(m_diffuseColor);
  changed |= texture->m_texture->Process(currentTime);

  return true;
}

bool CGUIImage::OnAction(const CAction &action)
{
  return false;
}

bool CGUIImage::OnMessage(CGUIMessage& message)
{
  if (message.GetMessage() == GUI_MSG_REFRESH_THUMBS)
  {
    if (!m_info.IsConstant())
      FreeTextures(true); // true as we want to free the texture immediately
    return true;
  }
  else if (message.GetMessage() == GUI_MSG_SET_FILENAME)
  {
    SetFileName(message.GetLabel());
    return true;
  }
  else if (message.GetMessage() == GUI_MSG_GET_FILENAME)
  {
    message.SetLabel(GetFileName());
    return true;
  }
  return CGUIControl::OnMessage(message);
}

void CGUIImage::AllocResources()
{
  if (m_texture->GetFileName().empty())
    return;

  CGUIControl::AllocResources();
  m_texture->AllocResources();
}

void CGUIImage::FreeTextures(bool immediately /* = false */)
{
  m_texture->FreeResources(immediately);
  for (unsigned int i = 0; i < m_fadingTextures.size(); i++)
    delete m_fadingTextures[i];
  m_fadingTextures.clear();
  m_currentTexture.clear();
  if (m_pendingBackground && m_info.IsConstant())
  {
    // A hidden constant image has no UpdateInfo call to restore its latest
    // filename. Preserve the queued request for its next resource allocation.
    m_texture->SetUseCache(m_pendingBackground->second);
    m_texture->SetFileName(m_pendingBackground->first);
  }
  m_pendingBackground.reset();
  if (!m_info.IsConstant()) // constant textures never change
    m_texture->SetFileName("");
}

void CGUIImage::FreeResources(bool immediately)
{
  FreeTextures(immediately);
  CGUIControl::FreeResources(immediately);
}

void CGUIImage::SetInvalid()
{
  m_texture->SetInvalid();
  CGUIControl::SetInvalid();
}

// WORKAROUND - we are currently resetting all animations when this is called, which shouldn't be the case
//              see CGUIControl::FreeResources() - this needs remedying.
void CGUIImage::FreeResourcesButNotAnims()
{
  FreeTextures();
  m_bAllocated=false;
  m_hasProcessed = false;
}

void CGUIImage::DynamicResourceAlloc(bool bOnOff)
{
  m_bDynamicResourceAlloc = bOnOff;
  m_texture->DynamicResourceAlloc(bOnOff);
  CGUIControl::DynamicResourceAlloc(bOnOff);
}

bool CGUIImage::CanFocus() const
{
  return false;
}

float CGUIImage::GetTextureWidth() const
{
  return m_texture->GetTextureWidth();
}

float CGUIImage::GetTextureHeight() const
{
  return m_texture->GetTextureHeight();
}

CRect CGUIImage::CalcRenderRegion() const
{
  CRect region = m_texture->GetRenderRect();

  for (const auto& itr : m_fadingTextures)
    region.Union(itr->m_texture->GetRenderRect());

  return CGUIControl::CalcRenderRegion().Intersect(region);
}

const std::string &CGUIImage::GetFileName() const
{
  return m_texture->GetFileName();
}

void CGUIImage::SetAspectRatio(const CAspectRatio &aspect)
{
  m_texture->SetAspectRatio(aspect);
}

void CGUIImage::SetCrossFade(unsigned int time)
{
  m_crossFadeTime = time;
  if (!m_crossFadeTime && m_texture->IsLazyLoaded() && !m_info.GetFallback().empty())
    m_crossFadeTime = 1;
}

void CGUIImage::SetFileName(const std::string& strFileName, bool setConstant, const bool useCache)
{
  if (setConstant)
    m_info.SetLabel(strFileName, "", GetParentID());

  // A transition runs until the outgoing image is gone and the current one is
  // at full strength.
  if (m_crossFadeTime && m_texture->ReadyToRender() && strFileName != m_currentTexture &&
      (!m_fadingTextures.empty() || m_currentFadeTime < m_crossFadeTime) &&
      (m_texture->IsBackgroundRequest() || m_texture->IsOpaque()) &&
      CServiceBroker::GetSettingsComponent()->GetAdvancedSettings()->m_guiLimitBackgroundFades)
  {
    // Never interrupt a visible background fade with a third image. Retain
    // only the latest selection, without starting its decode or upload yet.
    // Keeping the active pair avoids a brightness jump from dropping a layer.
    // An opaque image, such as a poster, likewise keeps one pair: it then
    // dissolves over an outgoing image at full strength and hides what is
    // behind both, where a third image would draw all of them see-through.
    m_pendingBackground = std::make_pair(strFileName, useCache);
    return;
  }
  // Returning to the current incoming image also withdraws a queued selection.
  m_pendingBackground.reset();
  m_texture->SetUseCache(useCache);

  if (m_crossFadeTime)
  {
    // set filename on the next texture
    if (m_currentTexture == strFileName)
      return; // nothing to do - we already have this image

    if (m_texture->ReadyToRender() || m_texture->GetFileName().empty())
    { // save the current image
      // A second outgoing image stacks the transition. Its layers are then
      // drawn see-through until it completes, however their opacity changes,
      // or an outgoing image that had partly faded would jump back to full
      // strength once the stack drops to one pair.
      for (CFadingTexture* texture : m_fadingTextures)
        texture->m_dissolve = false;
      auto* outgoing = new CFadingTexture(m_texture.get(), m_currentFadeTime);
      outgoing->m_dissolve = m_fadingTextures.empty() && m_currentFadeTime == m_crossFadeTime;
      m_fadingTextures.push_back(outgoing);
      MarkDirtyRegion();
    }
    m_currentFadeTime = 0;
  }
  if (m_currentTexture != strFileName)
  { // texture is changing - attempt to load it, and save the name in m_currentTexture.
    // we'll check whether it loaded or not in Render()
    m_currentTexture = strFileName;
    if (m_texture->SetFileName(m_currentTexture))
      MarkDirtyRegion();
  }
}

#ifdef _DEBUG
void CGUIImage::DumpTextureUse()
{
  if (m_texture->IsAllocated())
  {
    if (GetID())
      CLog::Log(LOGDEBUG, "Image control {} using texture {}", GetID(), m_texture->GetFileName());
    else
      CLog::Log(LOGDEBUG, "Using texture {}", m_texture->GetFileName());
  }
}
#endif

void CGUIImage::SetWidth(float width)
{
  m_texture->SetWidth(width);
  CGUIControl::SetWidth(m_texture->GetWidth());
}

void CGUIImage::SetHeight(float height)
{
  m_texture->SetHeight(height);
  CGUIControl::SetHeight(m_texture->GetHeight());
}

void CGUIImage::SetPosition(float posX, float posY)
{
  m_texture->SetPosition(posX, posY);
  CGUIControl::SetPosition(posX, posY);
}

void CGUIImage::SetInfo(const GUIINFO::CGUIInfoLabel &info)
{
  m_info = info;
  // a constant image never needs updating
  if (m_info.IsConstant())
    m_texture->SetFileName(m_info.GetLabel(0));
}

unsigned char CGUIImage::GetFadeLevel(unsigned int time) const
{
  float amount = (float)time / m_crossFadeTime;
  // we want a semi-transparent image, so we need to use a more complicated
  // fade technique.  Assuming a black background (not generally true, but still...)
  // we have
  // b(t) = [a - b(1-t)*a] / a*(1-b(1-t)*a),
  // where a = alpha, and b(t):[0,1] -> [0,1] is the blend function.
  // solving, we get
  // b(t) = [1 - (1-a)^t] / a
  const float alpha = 0.7f;
  return (unsigned char)(255.0f * (1 - pow(1-alpha, amount))/alpha);
}

std::string CGUIImage::GetDescription(void) const
{
  return GetFileName();
}

