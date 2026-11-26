#!/usr/bin/env python3
"""Run the video database subtitle-identity read/write and migration SQL in SQLite.

Requires Python 3, a C++17 compiler and SQLite 3.35+ development files.
"""
import pathlib
import re
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]


def method(source, signature):
    start = source.index(signature)
    end = source.index('\n}', start) + 2
    return source[start:end]


def fixture_source():
    db = (ROOT / 'xbmc/video/VideoDatabase.cpp').read_text()
    settings = (ROOT / 'xbmc/cores/VideoSettings.h').read_text()
    sqlite = (ROOT / 'xbmc/dbwrappers/sqlitedataset.cpp').read_text()
    create = re.search(r'm_pDS->exec\("CREATE TABLE settings .*?\);', db, re.S)[0]
    additions = '\n'.join(re.findall(r'  AddMissingColumn\("settings", .*?\);', db))
    fields = method(settings, 'class CVideoSettings\n').replace('CVideoSettings();',
                                                               'CVideoSettings() = default;')
    identity = method(settings, 'struct SubtitleIdentity')
    return (PREAMBLE + identity + ';\n' + fields + ';\n' +
            method(sqlite, 'std::string SqliteDatabase::vprepare(') + '\n' +
            DATABASE + method(db, 'static bool HasSubtitleIdentityColumns(') + '\n' +
            method(db, 'bool CVideoDatabase::GetVideoSettings(int') + '\n' +
            method(db, 'void CVideoDatabase::SetVideoSettings(int') + '\n' +
            method(db, 'bool CVideoDatabase::HasColumn(') + '\n' +
            method(db, 'void CVideoDatabase::AddMissingColumn(') + '\n' +
            'void CVideoDatabase::Create() {' + create + '}\n' +
            'void CVideoDatabase::Migrate() {' + additions + '}\n' + CASES)


PREAMBLE = r'''
#include <sqlite3.h>
#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>
using EINTERLACEMETHOD = int;
using ESCALINGMETHOD = int;
using ETONEMAPMETHOD = int;

struct SqliteDatabase { static std::string vprepare(const char*, va_list); };
enum { LOGERROR, LOGINFO };
struct CLog { template<class... Args> static void Log(Args&&...) {} };
'''


DATABASE = r'''
struct Value {
  sqlite3_stmt* s;
  int i;
  int get_asInt() const { return sqlite3_column_int(s, i); }
  bool get_asBool() const { return get_asInt() != 0; }
  float get_asFloat() const { return static_cast<float>(sqlite3_column_double(s, i)); }
  bool get_isNull() const { return sqlite3_column_type(s, i) == SQLITE_NULL; }
  std::string get_asString() const {
    const auto* text = sqlite3_column_text(s, i);
    return text ? reinterpret_cast<const char*>(text) : "";
  }
};
struct Dataset {
  sqlite3* db = nullptr;
  sqlite3_stmt* stmt = nullptr;
  int result = SQLITE_DONE;
  Dataset() { assert(sqlite3_open(":memory:", &db) == SQLITE_OK); }
  ~Dataset() { close(); sqlite3_close(db); }
  void exec(const std::string& sql) {
    if (sqlite3_exec(db, sql.c_str(), nullptr, nullptr, nullptr) != SQLITE_OK)
      throw std::runtime_error(sqlite3_errmsg(db));
  }
  void query(const std::string& sql) {
    close();
    if (sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, nullptr) != SQLITE_OK)
      throw std::runtime_error(sqlite3_errmsg(db));
    result = sqlite3_step(stmt);
    assert(result == SQLITE_ROW || result == SQLITE_DONE);
  }
  int num_rows() const { return result == SQLITE_ROW; }
  void close() { sqlite3_finalize(stmt); stmt = nullptr; }
  int fieldIndex(const char* name) const {
    for (int i = 0; i < sqlite3_column_count(stmt); ++i)
      if (std::string(sqlite3_column_name(stmt, i)) == name) return i;
    return -1;
  }
  Value fv(const char* name) const {
    const int i = fieldIndex(name);
    assert(i >= 0);
    return {stmt, i};
  }
};
struct CVideoDatabase {
  Dataset dataset;
  Dataset* m_pDS = &dataset;
  SqliteDatabase sqlite;
  SqliteDatabase* m_pDB = &sqlite;
  std::string PrepareSQL(const char* format, ...) const {
    va_list args;
    va_start(args, format);
    auto sql = sqlite.vprepare(format, args);
    va_end(args);
    return sql;
  }
  bool GetVideoSettings(int, CVideoSettings&);
  void SetVideoSettings(int, const CVideoSettings&);
  bool HasColumn(const std::string&, const std::string&);
  void AddMissingColumn(const std::string&, const std::string&, const std::string&);
  void Create();
  void Migrate();
};
'''


CASES = r'''
int main() {
  CVideoDatabase db;
  db.Create();
  // Simulate a pre-extension table, preserving all its original columns.
  db.dataset.exec("ALTER TABLE settings DROP COLUMN SubtitleExternal");
  db.dataset.exec("ALTER TABLE settings DROP COLUMN SubtitleLanguage");
  db.dataset.exec("ALTER TABLE settings DROP COLUMN SubtitleFilename");
  CVideoSettings chosen{}, loaded{};
  chosen.m_SubtitleStream = 3;
  chosen.m_SubtitleOn = true;
  chosen.m_subtitleIdentity = SubtitleIdentity{true, "dut", "L'été 100% (v2).srt"};
  db.SetVideoSettings(1, chosen);
  assert(db.GetVideoSettings(1, loaded));
  assert(!loaded.m_subtitleIdentity && loaded.m_SubtitleStream == 3 && loaded.m_SubtitleOn);
  db.dataset.exec("ALTER TABLE settings ADD SubtitleLanguage text");
  chosen.m_SubtitleStream = 4;
  db.SetVideoSettings(1, chosen);
  assert(db.GetVideoSettings(1, loaded) && loaded.m_SubtitleStream == 4);
  db.Migrate();
  db.Migrate();
  assert(db.GetVideoSettings(1, loaded) && !loaded.m_subtitleIdentity && loaded.m_SubtitleStream == 4);
  db.dataset.exec("UPDATE settings SET SubtitleStream=-1 WHERE idFile=1");
  assert(db.GetVideoSettings(1, loaded) && !loaded.m_subtitleIdentity && loaded.m_SubtitleStream == -1);

  db.SetVideoSettings(1, chosen); // UPDATE an existing row
  db.SetVideoSettings(2, chosen); // INSERT a new row
  for (int id : {1, 2}) {
    assert(db.GetVideoSettings(id, loaded));
    assert(loaded.m_SubtitleOn && loaded.m_SubtitleStream == -1 && loaded.m_subtitleIdentity);
    assert(loaded.m_subtitleIdentity->external && loaded.m_subtitleIdentity->language == "dut");
    assert(loaded.m_subtitleIdentity->filename == chosen.m_subtitleIdentity->filename);
  }
  db.dataset.exec("UPDATE settings SET SubtitleStream=2 WHERE idFile=1");
  assert(db.GetVideoSettings(1, loaded) && loaded.m_SubtitleStream == 2 && !loaded.m_subtitleIdentity);
  chosen.m_subtitleIdentity = SubtitleIdentity{false, "eng", ""};
  db.SetVideoSettings(1, chosen);
  assert(db.GetVideoSettings(1, loaded) && loaded.m_subtitleIdentity && !loaded.m_subtitleIdentity->external);
  assert(loaded.m_subtitleIdentity->language == "eng" && loaded.m_subtitleIdentity->filename.empty());
  chosen.m_subtitleIdentity.reset();
  chosen.m_SubtitleOn = false;
  for (int id : {1, 3}) {
    db.SetVideoSettings(id, chosen);
    assert(db.GetVideoSettings(id, loaded));
    assert(!loaded.m_SubtitleOn && !loaded.m_subtitleIdentity && loaded.m_SubtitleStream == 4);
    db.dataset.query("SELECT * FROM settings WHERE idFile=" + std::to_string(id));
    assert(db.dataset.fv("SubtitleExternal").get_isNull());
    db.dataset.close();
  }
  assert(!db.GetVideoSettings(9, loaded));
  std::puts("SQLite subtitle identity: creation, partial schema, migration, insert, update and stock numeric override passed");
}
'''


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='subtitle-settings-db-') as directory:
        path = pathlib.Path(directory)
        source = path / 'test.cpp'
        binary = path / 'test'
        source.write_text(fixture_source())
        subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', str(source),
                        '-lsqlite3', '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
