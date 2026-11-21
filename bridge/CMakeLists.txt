cmake_minimum_required(VERSION 3.28)
project(fc_bridge LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The CES client engine (ceslib), pinned to a ces commit. To build against a
# checkout, configure with -DFETCHCONTENT_SOURCE_DIR_CES=<ces checkout>.
include(FetchContent)
FetchContent_Declare(ces
  GIT_REPOSITORY https://github.com/fcecin/ces
  GIT_TAG        b855cf1ae20dc072b0151a2f3bc91a0be6e115aa)
set(CES_CLIENT_ONLY ON CACHE BOOL "" FORCE)
set(CES_BUILD_TESTS OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(ces)

find_package(Boost 1.83 REQUIRED)

add_executable(fc-bridge
  src/main.cpp
  src/seal.cpp
  src/cesops.cpp
  src/ceshops.cpp
  src/dialpool.cpp
  src/fileops.cpp
  src/computeops.cpp
  src/thinkops.cpp
  src/jsonimpl.cpp
  # The client side of ceshd (the ceshd/1 wire).
  ${ces_SOURCE_DIR}/src/ceshd/ceshd.cpp)
target_include_directories(fc-bridge PRIVATE src ${ces_SOURCE_DIR}/src/ceshd)
target_link_libraries(fc-bridge PRIVATE ceslib Boost::headers)
