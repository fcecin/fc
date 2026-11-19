# fc miner: a Qt app that mines on the site's CES server and moves the credit
# to the person's account on the site's ledger. It links ces's portable client
# subset (CES_CLIENT_ONLY), which ces builds on Linux, macOS and Windows.
#
#   cmake -B build -DCMAKE_PREFIX_PATH=<Qt 6>/gcc_64 [-DCES_SOURCE_DIR=<ces>]
#   cmake --build build -j
#
# Without CES_SOURCE_DIR, ces comes from GitHub at CES_TAG.
cmake_minimum_required(VERSION 3.24)
project(fc-miner VERSION 0.1 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# FC_STATIC makes one executable, with a static Qt from CMAKE_PREFIX_PATH,
# and Boost and the C++ runtime linked in. On Linux, the C library and the
# desktop libraries (X11, GL, fontconfig) stay shared, since every desktop has
# them and loads its drivers through them.
option(FC_STATIC "link the miner into one executable" OFF)
# The installation that the miner works for, its fc site and its mine server,
# built into the executable.
set(FC_SITE "" CACHE STRING "the fc site, as https://host")
set(FC_MINE_SERVER "" CACHE STRING "the CES server where the miner mines, as host:port")
if(NOT FC_SITE OR NOT FC_MINE_SERVER)
  message(FATAL_ERROR "Give the installation: -DFC_SITE=https://<site> -DFC_MINE_SERVER=<host>:<port>")
endif()
if(FC_STATIC)
  set(Boost_USE_STATIC_LIBS ON CACHE BOOL "" FORCE)
  set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
  # The TLS of a static Qt, for https to the site.
  set(OPENSSL_USE_STATIC_LIBS ON CACHE BOOL "" FORCE)
  if(MINGW)
    # A static Qt links freetype, png, zlib and more, so take their .a files.
    # Their own dependencies go last on the link line: bzip2 for freetype, and
    # the sockets and crypto of Windows for OpenSSL.
    set(CMAKE_FIND_LIBRARY_SUFFIXES ".a")
    string(APPEND CMAKE_CXX_STANDARD_LIBRARIES " -lbz2 -lws2_32 -lcrypt32 -lbcrypt")
    # harfbuzz's own CMake config names its DLL, so Qt takes harfbuzz from
    # pkg-config instead, which gives the static library and its dependencies.
    set(CMAKE_DISABLE_FIND_PACKAGE_harfbuzz ON)
    set(PKG_CONFIG_ARGN --static)
  endif()
endif()

set(CES_CLIENT_ONLY ON CACHE BOOL "" FORCE)
# A release runs on any x86-64 CPU. RandomX picks its CPU features at runtime.
set(CES_MARCH "x86-64" CACHE STRING "")
set(CES_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(CES_TAG "927a153" CACHE STRING "the commit of ces to build")
if(CES_SOURCE_DIR)
  add_subdirectory(${CES_SOURCE_DIR} ces EXCLUDE_FROM_ALL)
else()
  include(FetchContent)
  FetchContent_Declare(ces GIT_REPOSITORY https://github.com/fcecin/ces.git GIT_TAG ${CES_TAG})
  FetchContent_GetProperties(ces)
  if(NOT ces_POPULATED)
    FetchContent_Populate(ces)
    add_subdirectory(${ces_SOURCE_DIR} ${ces_BINARY_DIR} EXCLUDE_FROM_ALL)
  endif()
endif()

find_package(Qt6 REQUIRED COMPONENTS Widgets Network)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)

# qt_add_executable links the plugins that a static Qt needs, the platform
# and the TLS backend for https.
qt_add_executable(fc-miner WIN32 MACOSX_BUNDLE
  src/main.cpp
  src/engine.cpp src/engine.h
  src/lcd.cpp src/lcd.h
  src/site.cpp src/site.h
  src/window.cpp src/window.h
  src/miner.qrc)
target_link_libraries(fc-miner PRIVATE ceslib Qt6::Widgets Qt6::Network)
target_compile_definitions(fc-miner PRIVATE FC_SITE="${FC_SITE}" FC_MINE_SERVER="${FC_MINE_SERVER}")
if(FC_STATIC)
  # The miner reads only its PNG icon, which QtGui decodes itself.
  qt_import_plugins(fc-miner EXCLUDE_BY_TYPE imageformats)
endif()
set_target_properties(fc-miner PROPERTIES MACOSX_BUNDLE_BUNDLE_NAME "fc miner")
# The test of the display's digits: ctest --test-dir build
enable_testing()
add_executable(fc-miner-test tests/lcd_test.cpp src/lcd.cpp src/lcd.h)
target_link_libraries(fc-miner-test PRIVATE Qt6::Widgets)
add_test(NAME lcd COMMAND fc-miner-test)

if(FC_STATIC)
  if(MINGW)
    target_link_options(fc-miner PRIVATE -static -static-libgcc -static-libstdc++)
  elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_options(fc-miner PRIVATE -static-libgcc -static-libstdc++)
  endif()
endif()
