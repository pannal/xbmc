/*
 *  Copyright (C) 2026-present Team CoreELEC (https://coreelec.org)
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "OmniphonyConfig.h"

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <utility>
#include <vector>

namespace
{

/*
 * A YAML value as far as merging needs one. Scalars and lists are kept as
 * written and written back the same way: only mappings are merged, so only
 * mappings are taken apart - a list replaces whole, and its items, which can
 * be mappings of their own, never need reading.
 */
struct Node
{
  enum class Kind
  {
    Null,
    Scalar,
    Map,
    List,
  };
  Kind kind{Kind::Null};
  //! A scalar, or a one-line [ ] list, as written.
  std::string text;
  //! A "- " list: its lines, indented from the list's own column.
  std::vector<std::string> lines;
  std::vector<std::pair<std::string, Node>> map;

  Node* Find(const std::string& key)
  {
    for (auto& [k, v] : map)
      if (k == key)
        return &v;
    return nullptr;
  }
  const Node* Find(const std::string& key) const
  {
    for (const auto& [k, v] : map)
      if (k == key)
        return &v;
    return nullptr;
  }
  void Erase(const std::string& key)
  {
    map.erase(
        std::remove_if(map.begin(), map.end(), [&](const auto& kv) { return kv.first == key; }),
        map.end());
  }
};

struct Line
{
  int number;
  int indent;
  std::string text;
};

bool IsNullWord(const std::string& s)
{
  return s == "null" || s == "Null" || s == "NULL" || s == "~";
}

//! Past the quoted scalar opening at \p from, or npos when it does not close.
size_t SkipQuoted(const std::string& s, size_t from)
{
  const char quote = s[from];
  for (size_t i = from + 1; i < s.size(); ++i)
  {
    if (quote == '"' && s[i] == '\\')
      ++i;
    else if (s[i] == quote)
    {
      if (quote == '\'' && i + 1 < s.size() && s[i + 1] == '\'')
        ++i;
      else
        return i + 1;
    }
  }
  return std::string::npos;
}

//! A quote opens a scalar only where a value starts - at the start of the
//! line, or after `:`, `-`, `[`, `{` or `,` and spaces: "it's" and
//! "x 'y" are plain.
bool TokenStart(const std::string& s, size_t i)
{
  while (i > 0 && (s[i - 1] == ' ' || s[i - 1] == '\t'))
    --i;
  return i == 0 || std::string(":-[{,?").find(s[i - 1]) != std::string::npos;
}

//! Past the [ ] or { } collection opening at \p from, or npos.
size_t SkipCollection(const std::string& s, size_t from)
{
  int depth = 0;
  for (size_t i = from; i < s.size(); ++i)
  {
    const char c = s[i];
    if ((c == '"' || c == '\'') && TokenStart(s, i))
    {
      const size_t end = SkipQuoted(s, i);
      if (end == std::string::npos)
        return end;
      i = end - 1;
    }
    else if (c == '[' || c == '{')
      ++depth;
    else if ((c == ']' || c == '}') && --depth == 0)
      return i + 1;
  }
  return std::string::npos;
}

std::string StripComment(const std::string& s)
{
  for (size_t i = 0; i < s.size(); ++i)
  {
    const char c = s[i];
    if ((c == '"' || c == '\'') && TokenStart(s, i))
    {
      const size_t end = SkipQuoted(s, i);
      if (end == std::string::npos)
        return s; // refused where the value is read
      i = end - 1;
    }
    else if (c == '#' && (i == 0 || s[i - 1] == ' ' || s[i - 1] == '\t'))
      return s.substr(0, i);
  }
  return s;
}

std::string Trim(const std::string& s)
{
  const size_t b = s.find_first_not_of(" \t");
  if (b == std::string::npos)
    return "";
  return s.substr(b, s.find_last_not_of(" \t") - b + 1);
}

bool IsItem(const std::string& text)
{
  return text == "-" || text.compare(0, 2, "- ") == 0;
}

void PutUtf8(uint32_t c, std::string& out)
{
  if (c < 0x80)
    out += static_cast<char>(c);
  else if (c < 0x800)
  {
    out += static_cast<char>(0xC0 | (c >> 6));
    out += static_cast<char>(0x80 | (c & 0x3F));
  }
  else if (c < 0x10000)
  {
    out += static_cast<char>(0xE0 | (c >> 12));
    out += static_cast<char>(0x80 | ((c >> 6) & 0x3F));
    out += static_cast<char>(0x80 | (c & 0x3F));
  }
  else
  {
    out += static_cast<char>(0xF0 | (c >> 18));
    out += static_cast<char>(0x80 | ((c >> 12) & 0x3F));
    out += static_cast<char>(0x80 | ((c >> 6) & 0x3F));
    out += static_cast<char>(0x80 | (c & 0x3F));
  }
}

/*!
 * A double-quoted scalar's text, its escapes decoded as YAML 1.2 does, or
 * false for an escape YAML does not have. Decoded here, once, so that what
 * Kodi checks a value for and what it writes out are the same text - it is
 * written single-quoted, where nothing is an escape.
 */
bool DecodeDoubleQuoted(const std::string& text, std::string& out)
{
  out.clear();
  for (size_t i = 1; i + 1 < text.size(); ++i)
  {
    if (text[i] != '\\')
    {
      out += text[i];
      continue;
    }
    if (i + 2 >= text.size())
      return false;
    const char e = text[++i];
    int digits = 0;
    switch (e)
    {
      case '0':
        out += '\0';
        break;
      case 'a':
        out += '\a';
        break;
      case 'b':
        out += '\b';
        break;
      case 't':
      case '\t':
        out += '\t';
        break;
      case 'n':
        out += '\n';
        break;
      case 'v':
        out += '\v';
        break;
      case 'f':
        out += '\f';
        break;
      case 'r':
        out += '\r';
        break;
      case 'e':
        out += '\x1B';
        break;
      case ' ':
      case '"':
      case '/':
      case '\\':
        out += e;
        break;
      case 'N':
        PutUtf8(0x85, out);
        break;
      case '_':
        PutUtf8(0xA0, out);
        break;
      case 'L':
        PutUtf8(0x2028, out);
        break;
      case 'P':
        PutUtf8(0x2029, out);
        break;
      case 'x':
        digits = 2;
        break;
      case 'u':
        digits = 4;
        break;
      case 'U':
        digits = 8;
        break;
      default:
        return false;
    }
    if (!digits)
      continue;
    if (i + digits >= text.size() - 1) // the digits run into the closing quote
      return false;
    uint32_t c = 0;
    for (int d = 0; d < digits; ++d)
    {
      const char h = text[++i];
      if (!std::isxdigit(static_cast<unsigned char>(h)))
        return false;
      c = c * 16 +
          static_cast<uint32_t>(std::isdigit(static_cast<unsigned char>(h))
                                    ? h - '0'
                                    : std::tolower(static_cast<unsigned char>(h)) - 'a' + 10);
    }
    if (c > 0x10FFFF || (c >= 0xD800 && c <= 0xDFFF))
      return false;
    PutUtf8(c, out);
  }
  return true;
}

std::string Unquote(const std::string& text)
{
  if (text.size() >= 2 && text.front() == '\'')
  {
    std::string out;
    for (size_t i = 1; i + 1 < text.size(); ++i)
    {
      out += text[i];
      if (text[i] == '\'')
        ++i;
    }
    return out;
  }
  std::string out;
  if (text.size() >= 2 && text.front() == '"' && DecodeDoubleQuoted(text, out))
    return out;
  return text;
}

std::string Lower(std::string s)
{
  std::transform(s.begin(), s.end(), s.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return Trim(s);
}

std::string Quote(const std::string& value)
{
  std::string quoted = "'";
  for (const char c : value)
  {
    quoted += c;
    if (c == '\'')
      quoted += '\'';
  }
  return quoted + "'";
}

class CParser
{
public:
  explicit CParser(const std::string& text) { Split(text); }

  bool Parse(Node& root)
  {
    root = Node{};
    if (!m_error.empty())
      return false;
    if (m_lines.empty())
      return true;
    size_t i = 0;
    root.kind = Node::Kind::Map;
    if (!ParseMap(i, m_lines[0].indent, root))
      return false;
    if (i < m_lines.size())
      return Fail(m_lines[i].number, "indented less than the first key");
    return true;
  }

  const std::string& Error() const { return m_error; }

private:
  void Split(const std::string& text)
  {
    size_t from = text.compare(0, 3, "\xEF\xBB\xBF") == 0 ? 3 : 0;
    int number = 0;
    bool content = false;
    bool ended = false;
    while (from <= text.size() && m_error.empty())
    {
      size_t end = text.find('\n', from);
      if (end == std::string::npos)
        end = text.size();
      std::string raw = text.substr(from, end - from);
      from = end + 1;
      ++number;
      if (!raw.empty() && raw.back() == '\r')
        raw.pop_back();
      const std::string line = StripComment(raw);
      const size_t first = line.find_first_not_of(" \t");
      if (first == std::string::npos)
        continue;
      if (line.find('\t') < first)
      {
        Fail(number, "a tab in the indentation; indent with spaces");
        return;
      }
      const std::string body = Trim(line);
      if (ended)
      {
        Fail(number, "one document only");
        return;
      }
      if (first == 0 && (body == "---" || body.compare(0, 4, "--- ") == 0))
      {
        if (content || body != "---")
          Fail(number, "one document only");
        content = true;
        continue;
      }
      // The document's end marker: nothing may follow it.
      if (first == 0 && body == "...")
      {
        ended = true;
        continue;
      }
      if (first == 0 && body[0] == '%')
      {
        Fail(number, "one document only, without directives");
        return;
      }
      content = true;
      m_lines.push_back({number, static_cast<int>(first), body});
    }
  }

  bool Fail(int number, const std::string& why)
  {
    if (m_error.empty())
      m_error = "line " + std::to_string(number) + ": " + why;
    return false;
  }

  //! "key: rest", with the key unquoted.
  bool SplitKey(const Line& line, std::string& key, std::string& rest)
  {
    const std::string& s = line.text;
    size_t colon;
    if (s[0] == '"' || s[0] == '\'')
    {
      const size_t end = SkipQuoted(s, 0);
      colon = end == std::string::npos ? end : s.find_first_not_of(' ', end);
      if (colon == std::string::npos || s[colon] != ':')
        return Fail(line.number, "not `key: value`");
      key = Unquote(s.substr(0, end));
      if (s[0] == '"' && !DecodeDoubleQuoted(s.substr(0, end), key))
        return Fail(line.number, "an escape YAML does not have in " + s.substr(0, end));
    }
    else
    {
      if (std::string("?&*!|>{[-@`%").find(s[0]) != std::string::npos)
        return Fail(line.number, "not `key: value`");
      colon = s.find(':');
      while (colon != std::string::npos && colon + 1 < s.size() && s[colon + 1] != ' ')
        colon = s.find(':', colon + 1);
      if (colon == std::string::npos)
        return Fail(line.number, "not `key: value`");
      key = Trim(s.substr(0, colon));
    }
    if (key.empty())
      return Fail(line.number, "an empty key");
    if (colon + 1 < s.size() && s[colon + 1] != ' ')
      return Fail(line.number, "not `key: value`");
    rest = Trim(s.substr(colon + 1));
    return true;
  }

  bool ParseMap(size_t& i, int indent, Node& out)
  {
    while (i < m_lines.size())
    {
      const Line& line = m_lines[i];
      if (line.indent < indent)
        return true;
      if (line.indent > indent)
        return Fail(line.number, "indented more than the key above it");
      if (IsItem(line.text))
        return Fail(line.number, "a list item where a key was expected");
      std::string key, rest;
      if (!SplitKey(line, key, rest))
        return false;
      if (out.Find(key))
        return Fail(line.number, key + " appears twice");
      ++i;
      Node value;
      if (!rest.empty())
      {
        if (!ParseInline(rest, line.number, value))
          return false;
        if (i < m_lines.size() && m_lines[i].indent > indent)
          return Fail(m_lines[i].number, "a value continued on another line; write it on one");
      }
      else if (i < m_lines.size() && m_lines[i].indent > indent)
      {
        if (IsItem(m_lines[i].text))
          ParseList(i, indent, false, value);
        else
        {
          value.kind = Node::Kind::Map;
          if (!ParseMap(i, m_lines[i].indent, value))
            return false;
        }
      }
      else if (i < m_lines.size() && m_lines[i].indent == indent && IsItem(m_lines[i].text))
        ParseList(i, indent, true, value);
      out.map.emplace_back(std::move(key), std::move(value));
    }
    return true;
  }

  //! A "- " list, kept as its lines: every line deeper than its key, and
  //! the items at the key's own column when it is written there.
  void ParseList(size_t& i, int keyIndent, bool atKey, Node& out)
  {
    out.kind = Node::Kind::List;
    const int base = m_lines[i].indent;
    while (i < m_lines.size())
    {
      const Line& line = m_lines[i];
      const bool deeper = line.indent > keyIndent;
      const bool item = atKey && line.indent == keyIndent && IsItem(line.text);
      if (!deeper && !item)
        break;
      out.lines.push_back(std::string(std::max(0, line.indent - base), ' ') + line.text);
      ++i;
    }
  }

  bool ParseInline(const std::string& s, int number, Node& out)
  {
    const char c = s[0];
    if (c == '&' || c == '*' || c == '!')
      return Fail(number, "anchors, aliases and tags are not read");
    if (c == '|' || c == '>')
      return Fail(number, "a multi-line scalar; write the value on one line");
    if (c == '{')
    {
      size_t pos = 0;
      if (!ParseFlowMap(s, pos, number, out))
        return false;
      if (pos != s.size())
        return Fail(number, "something after the closing }");
      return true;
    }
    if (c == '[')
    {
      const size_t end = SkipCollection(s, 0);
      if (end == std::string::npos)
        return Fail(number, "a [ ] list must close on its line");
      if (end != s.size())
        return Fail(number, "something after the closing ]");
      out.kind = Node::Kind::List;
      out.text = s;
      return true;
    }
    if (c == '"' || c == '\'')
    {
      const size_t end = SkipQuoted(s, 0);
      if (end == std::string::npos)
        return Fail(number, "a quoted value must close on its line");
      if (end != s.size())
        return Fail(number, "something after the closing quote");
      return Quoted(s, number, out);
    }
    if (s.find(": ") != std::string::npos || s.back() == ':')
      return Fail(number, "`key: value` inside a value");
    if (IsNullWord(s))
      return true;
    out.kind = Node::Kind::Scalar;
    out.text = s;
    return true;
  }

  //! A quoted scalar, kept single-quoted: a double-quoted one is decoded
  //! once, so that what is checked is what is written.
  bool Quoted(const std::string& text, int number, Node& out)
  {
    out.kind = Node::Kind::Scalar;
    out.text = text;
    if (text[0] != '"')
      return true;
    std::string decoded;
    if (!DecodeDoubleQuoted(text, decoded))
      return Fail(number, "an escape YAML does not have in " + text);
    out.text = Quote(decoded);
    return true;
  }

  //! A one-line { } mapping at \p pos, taken apart so that it merges.
  bool ParseFlowMap(const std::string& s, size_t& pos, int number, Node& out)
  {
    const std::string open = "a { } mapping must close on its line";
    out.kind = Node::Kind::Map;
    ++pos;
    const auto skip = [&]
    {
      while (pos < s.size() && s[pos] == ' ')
        ++pos;
    };
    skip();
    if (pos < s.size() && s[pos] == '}')
    {
      ++pos;
      return true;
    }
    while (pos < s.size())
    {
      std::string key;
      if (s[pos] == '"' || s[pos] == '\'')
      {
        const size_t end = SkipQuoted(s, pos);
        if (end == std::string::npos)
          return Fail(number, open);
        key = Unquote(s.substr(pos, end - pos));
        if (s[pos] == '"' && !DecodeDoubleQuoted(s.substr(pos, end - pos), key))
          return Fail(number, "an escape YAML does not have in a key");
        pos = end;
        while (pos < s.size() && s[pos] == ' ')
          ++pos;
      }
      else
      {
        const size_t end = s.find_first_of(":,{}[]", pos);
        if (end == std::string::npos)
          return Fail(number, open);
        key = Trim(s.substr(pos, end - pos));
        pos = end;
        // After a plain key, YAML reads `a:b` as one key: the colon needs a
        // space, or the value's end, after it.
        if (pos + 1 < s.size() && std::string(" ,}").find(s[pos + 1]) == std::string::npos)
          return Fail(number, "not `key: value` in a { } mapping (a space after the colon)");
      }
      if (key.empty() || pos >= s.size() || s[pos] != ':')
        return Fail(number, "not `key: value` in a { } mapping");
      if (out.Find(key))
        return Fail(number, key + " appears twice");
      ++pos;
      skip();
      Node value;
      if (pos >= s.size())
        return Fail(number, open);
      if (s[pos] == '{')
      {
        if (!ParseFlowMap(s, pos, number, value))
          return false;
      }
      else if (s[pos] == '[')
      {
        const size_t end = SkipCollection(s, pos);
        if (end == std::string::npos)
          return Fail(number, open);
        value.kind = Node::Kind::List;
        value.text = s.substr(pos, end - pos);
        pos = end;
      }
      else if (s[pos] == '"' || s[pos] == '\'')
      {
        const size_t end = SkipQuoted(s, pos);
        if (end == std::string::npos)
          return Fail(number, open);
        if (!Quoted(s.substr(pos, end - pos), number, value))
          return false;
        pos = end;
      }
      else
      {
        const size_t end = s.find_first_of(",}", pos);
        if (end == std::string::npos)
          return Fail(number, open);
        const std::string text = Trim(s.substr(pos, end - pos));
        if (!text.empty() && (std::string("&*!|>").find(text[0]) != std::string::npos ||
                              text.find(": ") != std::string::npos))
          return Fail(number, "not `key: value` in a { } mapping");
        // `{ a: , b: 1 }`: an empty value is null.
        if (!text.empty() && !IsNullWord(text))
        {
          value.kind = Node::Kind::Scalar;
          value.text = text;
        }
        pos = end;
      }
      out.map.emplace_back(std::move(key), std::move(value));
      skip();
      if (pos < s.size() && s[pos] == ',')
      {
        ++pos;
        skip();
        // A trailing comma: `{ a: 1, }`.
        if (pos < s.size() && s[pos] == '}')
        {
          ++pos;
          return true;
        }
        continue;
      }
      if (pos < s.size() && s[pos] == '}')
      {
        ++pos;
        return true;
      }
      return Fail(number, open);
    }
    return Fail(number, open);
  }

  std::vector<Line> m_lines;
  std::string m_error;
};

std::string EmitKey(const std::string& key)
{
  const bool plain = std::all_of(key.begin(), key.end(), [](unsigned char c)
                                 { return std::isalnum(c) || c == '_' || c == '-'; });
  return plain ? key : Quote(key);
}

void Emit(const Node& map, int indent, std::string& out)
{
  const std::string pad(indent, ' ');
  for (const auto& [key, value] : map.map)
  {
    out += pad + EmitKey(key) + ":";
    switch (value.kind)
    {
      case Node::Kind::Null:
        out += " null\n";
        break;
      case Node::Kind::Scalar:
        out += " " + value.text + "\n";
        break;
      case Node::Kind::Map:
        if (value.map.empty())
          out += " {}\n";
        else
        {
          out += "\n";
          Emit(value, indent + 2, out);
        }
        break;
      case Node::Kind::List:
        if (value.lines.empty())
          out += " " + value.text + "\n";
        else
        {
          out += "\n";
          for (const std::string& line : value.lines)
            out += pad + "  " + line + "\n";
        }
        break;
    }
  }
}

//! Without its nulls: they inherit, and a mapping left empty sets nothing.
void StripNulls(Node& map)
{
  std::vector<std::pair<std::string, Node>> kept;
  for (auto& [key, value] : map.map)
  {
    if (value.kind == Node::Kind::Map)
    {
      StripNulls(value);
      if (value.map.empty())
        continue;
    }
    if (value.kind != Node::Kind::Null)
      kept.emplace_back(std::move(key), std::move(value));
  }
  map.map = std::move(kept);
}

void Merge(Node& base, const Node& over)
{
  for (const auto& [key, value] : over.map)
  {
    Node* was = base.Find(key);
    if (was && was->kind == Node::Kind::Map && value.kind == Node::Kind::Map)
      Merge(*was, value);
    else if (was)
      *was = value;
    else
      base.map.emplace_back(key, value);
  }
}

const char* const HOST_OWNED =
    "is Kodi's (the decoder, the input or output, or OSC), not a rendering control";

//! Why Kodi keeps a render key, or nullptr for one the listener may set.
const char* HostOwned(const std::string& key)
{
  static const char* const owned[] = {"output_channel_mapping",
                                      "input_mode",
                                      "input_pipe",
                                      "live_input",
                                      "output_backend",
                                      "output_file",
                                      "output_file_format",
                                      "output_device",
                                      "output_sample_rate",
                                      "latency_target",
                                      "bridge_path",
                                      "bridge_paths",
                                      "osc",
                                      "osc_metering",
                                      "osc_rx_port",
                                      "osc_host",
                                      "osc_port",
                                      "meter_rate",
                                      "diag_rate",
                                      "enable_adaptive_resampling"};
  for (const char* k : owned)
    if (key == k)
      return HOST_OWNED;
  if (key.compare(0, 20, "adaptive_resampling_") == 0)
    return HOST_OWNED;
  if (key == "presentation")
    return "is chosen by the decoder bridge in Kodi";
  if (key == "speaker_layout")
    return "(a layout file) is not read here; set current_layout instead";
  if (key == "enable_vbap" || key == "continuous" || key == "bed_conform")
    return "is read by the desktop player only and does nothing here";
  if (key == "vbap_table")
    return "names a precomputed table file, which Kodi does not use";
  if (key == "dialogue_gain_db")
    return "does nothing here: only IAMF streams tag dialogue channels, and Kodi plays none "
           "through this renderer";
  if (key == "virtual_bed" || key == "object_generator_params" || key == "phantom_enabled" ||
      key == "phantom_params")
    return "is a legacy key read for migration only";
  return nullptr;
}

std::string ScalarValue(const Node* node)
{
  return node && node->kind == Node::Kind::Scalar ? Lower(Unquote(node->text)) : "";
}

//! Why config.yaml cannot play, or empty; resolves its file paths.
std::string Refusal(Node& over, const std::string& dir, OmniphonyListenerConfig& result)
{
  for (const auto& [key, value] : over.map)
    if (key != "render" && key != "schema_version")
      return key + ": config.yaml holds `render:` (and `schema_version:`) only";
  Node* render = over.Find("render");
  if (!render)
    return "";
  if (render->kind != Node::Kind::Map)
    return "render: must be a mapping";
  for (const auto& [key, value] : render->map)
    if (const char* why = HostOwned(key))
      return "render." + key + " " + why;
  if (ScalarValue(render->Find("channel_render_mode")) == "host")
    return "render.channel_render_mode: host hands channel-based content back to the player, "
           "which Kodi does not take; spatial renders it";
  result.layoutSet = render->Find("current_layout") != nullptr;
  result.decodeThreadSet = render->Find("decode_thread") != nullptr;

  Node* bin = render->Find("binaural");
  if (!bin)
    return "";
  if (bin->kind != Node::Kind::Map)
    return "render.binaural must be a mapping";
  if (const Node* mode = bin->Find("output_mode"); mode && ScalarValue(mode) != "binaural")
    return "render.binaural.output_mode: Kodi renders to headphones only (binaural)";
  const std::string source = ScalarValue(bin->Find("hrir_source"));
  if (source.compare(0, 5, "sofa:") == 0 || source.compare(0, 5, "brir:") == 0)
    return "render.binaural.hrir_source: name the file in hrtf_sofa_path or brir_sofa_path";
  if (bin->Find("head_tracking"))
    return "render.binaural.head_tracking: Kodi has no head tracker";
  if (ScalarValue(bin->Find("brir_head_tracking")) == "true")
    return "render.binaural.brir_head_tracking: Kodi has no head tracker, and every "
           "orientation of a room would be kept in memory";
  if (bin->Find("hrtf_grid_cache"))
    return "render.binaural.hrtf_grid_cache: where the HRIR grid is kept is Kodi's";

  for (const char* key : {"hrtf_sofa_path", "brir_sofa_path"})
  {
    Node* path = bin->Find(key);
    if (!path)
      continue;
    const std::string at = std::string("render.binaural.") + key;
    if (path->kind != Node::Kind::Scalar)
      return at + " must be a path";
    std::string file = Trim(Unquote(path->text));
    if (file.empty())
      return at + " must name a file";
    if (file.find("://") != std::string::npos)
      return at + " " + file + ": a local path is needed (choose network files through Kodi)";
    if (file[0] != '/')
      file = dir + (!dir.empty() && dir.back() == '/' ? "" : "/") + file;
    path->text = Quote(file);
    if (std::string(key) == "brir_sofa_path")
      result.room = file;
  }
  return "";
}

} // namespace

OmniphonyListenerConfig OmniphonyMergeListenerConfig(const std::string& base,
                                                     const std::string& listener,
                                                     const std::string& listenerDir)
{
  using Status = OmniphonyListenerConfig::Status;
  OmniphonyListenerConfig result;
  const auto refuse = [&](const std::string& why)
  {
    OmniphonyListenerConfig refused;
    refused.status = Status::Rejected;
    refused.reason = why;
    return refused;
  };

  if (listener.size() > OMNI_LISTENER_CONFIG_MAX)
    return refuse("config.yaml is " + std::to_string(listener.size()) + " bytes, more than the " +
                  std::to_string(OMNI_LISTENER_CONFIG_MAX) + " read");
  Node over;
  CParser parser(listener);
  if (!parser.Parse(over))
    return refuse(parser.Error());
  StripNulls(over);
  if (over.map.empty())
    return result;

  const std::string why = Refusal(over, listenerDir, result);
  if (!why.empty())
    return refuse(why);

  Node merged;
  CParser baseParser(base);
  if (!baseParser.Parse(merged))
    return refuse("render.yaml, " + baseParser.Error());
  if (merged.kind != Node::Kind::Map)
    merged.kind = Node::Kind::Map;
  Merge(merged, over);

  Node* render = merged.Find("render");
  Node* bin = render ? render->Find("binaural") : nullptr;
  const bool room = ScalarValue(bin ? bin->Find("hrir_source") : nullptr) == "brir";
  if (result.layoutSet && room)
    return refuse("render.current_layout: a room renders on its own measured loudspeakers");
  // A room file named while no room is selected is not read: a path alone
  // does not select it.
  if (!room)
    result.room.clear();
  // Cascaded, a session needs virtual speakers: Kodi's twelve, unless
  // config.yaml names its own. A room is rendered on its own loudspeakers.
  result.cascade = !room && ScalarValue(bin ? bin->Find("mode") : nullptr) == "cascaded";
  // Kodi's grid is of Kodi's set: another set would be built over it at
  // every switch between the two.
  const Node* overRender = over.Find("render");
  const Node* overBin = overRender ? overRender->Find("binaural") : nullptr;
  if (overBin && overBin->Find("hrtf_sofa_path") && bin)
    bin->Erase("hrtf_grid_cache");
  // The response is the listener's when they choose one other than a room,
  // or a room of their own; "brir" alone keeps the settings' room.
  const Node* overSource = overBin ? overBin->Find("hrir_source") : nullptr;
  result.ownResponse = (overSource && ScalarValue(overSource) != "brir") || !result.room.empty();

  result.status = Status::Applied;
  result.yaml = "# Written by Kodi: render.yaml with config.yaml over it.\n"
                "# Edit config.yaml, not this file.\n";
  Emit(merged, 0, result.yaml);
  return result;
}
