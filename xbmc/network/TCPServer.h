/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "interfaces/json-rpc/IClient.h"
#include "interfaces/json-rpc/IJSONRPCAnnouncer.h"
#include "interfaces/json-rpc/ITransportLayer.h"
#include "threads/CriticalSection.h"
#include "threads/Thread.h"
#include "websocket/WebSocket.h"

#include <memory>
#include <vector>

#include <sys/socket.h>

#include "PlatformDefs.h"

class CVariant;

namespace JSONRPC
{
  class CTCPServer : public ITransportLayer, public JSONRPC::IJSONRPCAnnouncer, public CThread
  {
  public:
    static bool StartServer(int port, bool nonlocal);
    static void StopServer(bool bWait);
    static bool IsRunning();

    bool PrepareDownload(const char *path, CVariant &details, std::string &protocol) override;
    bool Download(const char *path, CVariant &result) override;
    int GetCapabilities() override;

    void Announce(ANNOUNCEMENT::AnnouncementFlag flag,
                  const std::string& sender,
                  const std::string& message,
                  const CVariant& data) override;

  protected:
    void Process() override;
  private:
    CTCPServer(int port, bool nonlocal);
    bool Initialize();
    bool InitializeBlue();
    bool InitializeTCP();
    void Deinitialize();

    class CTCPClient : public IClient
    {
    public:
      CTCPClient();
      //Copying a CCriticalSection is not allowed, so copy everything but that
      //when adding a member variable, make sure to copy it in CTCPClient::Copy
      CTCPClient(const CTCPClient& client);
      CTCPClient& operator=(const CTCPClient& client);
      ~CTCPClient() override = default;

      int GetPermissionFlags() override;
      int GetAnnouncementFlags() override;
      bool SetAnnouncementFlags(int flags) override;

      virtual void Send(const char *data, unsigned int size);
      virtual void PushBuffer(CTCPServer *host, const char *buffer, int length);
      virtual void Disconnect();

      virtual bool IsNew() const { return m_new; }
      virtual bool Closing() const { return false; }

      SOCKET m_socket;
      sockaddr_storage m_cliaddr;
      socklen_t m_addrlen;
      CCriticalSection m_critSection;

    protected:
      void Copy(const CTCPClient& client);
    private:
      bool m_new;
      int m_announcementflags;
      int m_beginBrackets, m_endBrackets;
      char m_beginChar, m_endChar;
      std::string m_buffer;
    };

    class CWebSocketClient : public CTCPClient
    {
    public:
      explicit CWebSocketClient(CWebSocket *websocket);
      CWebSocketClient(const CWebSocketClient& client);
      CWebSocketClient(CWebSocket *websocket, const CTCPClient& client);
      CWebSocketClient& operator=(const CWebSocketClient& client);
      ~CWebSocketClient() override;

      void Send(const char *data, unsigned int size) override;
      void PushBuffer(CTCPServer *host, const char *buffer, int length) override;
      void Disconnect() override;

      bool IsNew() const override { return m_websocket == NULL; }
      bool Closing() const override { return m_websocket != NULL && m_websocket->GetState() == WebSocketStateClosed; }

    private:
      CWebSocket *m_websocket;
      std::string m_buffer;
    };

    // Only the server thread changes m_connections. The lock orders those changes against
    // the announcement thread's snapshot; code never holds the lock while calling a client or
    // taking another lock. The shared_ptr keeps a client in a snapshot alive after removal.
    std::vector<std::shared_ptr<CTCPClient>> m_connections;
    CCriticalSection m_connectionsLock;
    std::vector<SOCKET> m_servers;
    int m_port;
    bool m_nonlocal;
    void* m_sdpd;

    static CTCPServer *ServerInstance;
  };
}
