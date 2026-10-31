cmake_minimum_required(VERSION 3.20)

project(sde
  VERSION 0.1.0
  DESCRIPTION "Smart Data Engine client library for C++"
  HOMEPAGE_URL "https://github.com/Smart-Data-Engines/smart-data-engine-sdk"
  LANGUAGES C CXX)

# A development build names a build, not a feature set (docs/publishing.md 5.1): every commit after
# a release reports this suffix until the next release sets a final number.
set(SDE_VERSION_SUFFIX "-dev")

list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")

# ISO C++20 everywhere, the tests included: no compiler extension the library could come to rely on.
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(SDE_BUILD_TESTS "Build the unit tests and the conformance runner" ${PROJECT_IS_TOP_LEVEL})
option(SDE_WERROR "Treat compiler warnings as errors" OFF)
option(SDE_POSTGRES "Build sde::postgres, the PostgreSQL adapter, over libpq" ON)
option(SDE_CLICKHOUSE "Build sde::clickhouse, the ClickHouse adapter, over libcurl" ON)
option(SDE_ORDERBOOK "Build sde::orderbook, the orderbook engine adapter, over its TCP protocol" ON)
set(SDE_SANITIZE "" CACHE STRING "Comma-separated sanitizers to build with, e.g. address,undefined")

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

# --- dependencies -----------------------------------------------------------------------------
# OpenSSL for SHA-256, HMAC-SHA256 and Ed25519 (and, in the adapters, TLS); utf8proc for NFC, the one
# dependency the format contract forces. Nothing else reaches the core library.
find_package(OpenSSL 3.0 REQUIRED COMPONENTS Crypto)
find_package(utf8proc REQUIRED)
# Each adapter's driver is that adapter's alone: the core links no network library.
if(SDE_POSTGRES)
  find_package(PostgreSQL REQUIRED)
endif()
if(SDE_CLICKHOUSE)
  find_package(CURL 7.77 REQUIRED)
endif()
if(SDE_ORDERBOOK)
  find_package(OpenSSL 3.0 REQUIRED COMPONENTS Crypto SSL)
endif()

# --- warnings and sanitizers, as an interface target every target here links ------------------
add_library(sde_build_flags INTERFACE)
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(sde_build_flags INTERFACE
    -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion -Wshadow -Wnon-virtual-dtor
    -Wold-style-cast -Woverloaded-virtual -Wcast-qual -Wformat=2 -Wimplicit-fallthrough)
  if(SDE_WERROR)
    target_compile_options(sde_build_flags INTERFACE -Werror)
  endif()
  if(SDE_SANITIZE)
    target_compile_options(sde_build_flags INTERFACE
      -fsanitize=${SDE_SANITIZE} -fno-omit-frame-pointer -fno-sanitize-recover=all)
    target_link_options(sde_build_flags INTERFACE -fsanitize=${SDE_SANITIZE})
  endif()
endif()

# --- the core library ---------------------------------------------------------------------------
add_library(sde
  src/version.cpp
  src/errors.cpp
  src/json.cpp
  src/unicode.cpp
  src/bulk.cpp
  src/canonical.cpp
  src/crypto.cpp
  src/encoding.cpp
  src/model.cpp
  src/neutral.cpp
  src/hashing.cpp
  src/python_compat.cpp
  src/python_url.cpp
  src/physical.cpp
  src/provisioning.cpp
  src/layout.cpp
  src/placement.cpp
  src/routing.cpp
  src/telemetry.cpp
  src/recorder.cpp
  src/bignum.cpp
  src/value.cpp
  src/schema.cpp
  src/query.cpp
  src/log.cpp
  src/write_fence.cpp
  src/watermark.cpp
  src/migration.cpp
  src/generation.cpp
  src/session.cpp
  src/verification.cpp
  src/packets.cpp
)
add_library(sde::sde ALIAS sde)
target_compile_features(sde PUBLIC cxx_std_20)
set_target_properties(sde PROPERTIES
  CXX_EXTENSIONS OFF
  VERSION ${PROJECT_VERSION}
  SOVERSION ${PROJECT_VERSION_MAJOR}.${PROJECT_VERSION_MINOR}
  EXPORT_NAME sde)
target_include_directories(sde
  PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
  PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_compile_definitions(sde PRIVATE
  SDE_VERSION_STRING="${PROJECT_VERSION}${SDE_VERSION_SUFFIX}")
target_link_libraries(sde
  PRIVATE OpenSSL::Crypto utf8proc::utf8proc $<BUILD_INTERFACE:sde_build_flags>)

# --- the in-memory engine, for the migration vectors and for anybody's adapter tests -------------
# A target of its own: an application links it in its tests, never in production.
add_library(sde_testing src/testing/memory.cpp)
add_library(sde::testing ALIAS sde_testing)
target_compile_features(sde_testing PUBLIC cxx_std_20)
set_target_properties(sde_testing PROPERTIES CXX_EXTENSIONS OFF EXPORT_NAME testing)
target_include_directories(sde_testing
  PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
  PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(sde_testing PUBLIC sde PRIVATE $<BUILD_INTERFACE:sde_build_flags>)

# --- the adapters: a target each, so an application links the drivers of the engines it uses ---
set(SDE_ADAPTERS "")
if(SDE_POSTGRES)
  add_library(sde_postgres
    src/engines/postgres.cpp
    src/engines/postgres/connection.cpp
    src/engines/postgres/fences.cpp
    src/engines/postgres/values.cpp)
  add_library(sde::postgres ALIAS sde_postgres)
  target_compile_features(sde_postgres PUBLIC cxx_std_20)
  set_target_properties(sde_postgres PROPERTIES CXX_EXTENSIONS OFF EXPORT_NAME postgres)
  target_include_directories(sde_postgres
    PUBLIC
      $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
      $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src)
  target_link_libraries(sde_postgres
    PUBLIC sde PRIVATE PostgreSQL::PostgreSQL $<BUILD_INTERFACE:sde_build_flags>)
  list(APPEND SDE_ADAPTERS sde_postgres)
endif()
if(SDE_CLICKHOUSE)
  add_library(sde_clickhouse
    src/engines/clickhouse.cpp
    src/engines/clickhouse/dsn.cpp
    src/engines/clickhouse/fences.cpp
    src/engines/clickhouse/http.cpp
    src/engines/clickhouse/values.cpp)
  add_library(sde::clickhouse ALIAS sde_clickhouse)
  target_compile_features(sde_clickhouse PUBLIC cxx_std_20)
  set_target_properties(sde_clickhouse PROPERTIES CXX_EXTENSIONS OFF EXPORT_NAME clickhouse)
  target_include_directories(sde_clickhouse
    PUBLIC
      $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
      $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src)
  target_link_libraries(sde_clickhouse
    PUBLIC sde PRIVATE CURL::libcurl OpenSSL::Crypto $<BUILD_INTERFACE:sde_build_flags>)
  list(APPEND SDE_ADAPTERS sde_clickhouse)
endif()
if(SDE_ORDERBOOK)
  add_library(sde_orderbook
    src/engines/orderbook.cpp
    src/engines/orderbook/dsn.cpp
    src/engines/orderbook/wire.cpp)
  add_library(sde::orderbook ALIAS sde_orderbook)
  target_compile_features(sde_orderbook PUBLIC cxx_std_20)
  set_target_properties(sde_orderbook PROPERTIES CXX_EXTENSIONS OFF EXPORT_NAME orderbook)
  target_include_directories(sde_orderbook
    PUBLIC
      $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
      $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src)
  target_link_libraries(sde_orderbook
    PUBLIC sde PRIVATE OpenSSL::SSL OpenSSL::Crypto $<BUILD_INTERFACE:sde_build_flags>)
  list(APPEND SDE_ADAPTERS sde_orderbook)
endif()

# --- installation: find_package(sde CONFIG) -------------------------------------------------------
install(TARGETS sde sde_testing ${SDE_ADAPTERS} EXPORT sdeTargets
  ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
  RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
install(DIRECTORY include/sde DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(EXPORT sdeTargets NAMESPACE sde:: DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/sde)
install(FILES cmake/Findutf8proc.cmake DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/sde)
configure_package_config_file(cmake/sdeConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/sdeConfig.cmake
  INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/sde)
write_basic_package_version_file(${CMAKE_CURRENT_BINARY_DIR}/sdeConfigVersion.cmake
  COMPATIBILITY SameMinorVersion)
install(FILES
  ${CMAKE_CURRENT_BINARY_DIR}/sdeConfig.cmake
  ${CMAKE_CURRENT_BINARY_DIR}/sdeConfigVersion.cmake
  DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/sde)

# --- tests --------------------------------------------------------------------------------------
if(SDE_BUILD_TESTS)
  enable_testing()
  add_subdirectory(tests)
  add_subdirectory(examples)
  add_test(NAME example.route COMMAND sde_example_route)
endif()
