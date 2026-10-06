find_package(GTest CONFIG QUIET)
if(NOT GTest_FOUND)
  # Pinned by its archive's digest, so a build that fetches it gets exactly the code we test with.
  include(FetchContent)
  FetchContent_Declare(googletest
    URL https://github.com/google/googletest/releases/download/v1.15.2/googletest-1.15.2.tar.gz
    URL_HASH SHA256=7b42b4d6ed48810c5362c265a17faebe90dc2373c885e5216439d37927f02926)
  set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
  FetchContent_MakeAvailable(googletest)
endif()

include(GoogleTest)

# The shared vectors, read in place: every library runs the same files in its own CI.
set(SDE_VECTORS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../conformance/vectors")
get_filename_component(SDE_VECTORS_DIR "${SDE_VECTORS_DIR}" ABSOLUTE)

add_library(sde_test_support STATIC support/vectors.cpp)
target_include_directories(sde_test_support
  PUBLIC ${CMAKE_CURRENT_SOURCE_DIR} ${CMAKE_CURRENT_SOURCE_DIR}/../src)
target_compile_definitions(sde_test_support PUBLIC SDE_VECTORS_DIR="${SDE_VECTORS_DIR}")
target_link_libraries(sde_test_support PUBLIC sde::sde GTest::gtest sde_build_flags)

add_executable(sde_unit_tests
  unit/test_json.cpp
  unit/test_unicode.cpp
  unit/test_crypto.cpp
  unit/test_encoding.cpp
  unit/test_python_compat.cpp
  unit/test_layout.cpp
  unit/test_placement.cpp
)
target_link_libraries(sde_unit_tests PRIVATE sde_test_support GTest::gtest_main
  OpenSSL::Crypto)
gtest_discover_tests(sde_unit_tests DISCOVERY_MODE PRE_TEST)

add_executable(sde_conformance
  conformance/stages.cpp
  conformance/test_canonical.cpp
  conformance/test_model.cpp
  conformance/test_errors.cpp
  conformance/test_hashing.cpp
  conformance/test_signature.cpp
  conformance/test_routing.cpp
)
target_link_libraries(sde_conformance PRIVATE sde_test_support GTest::gtest_main)
gtest_discover_tests(sde_conformance DISCOVERY_MODE PRE_TEST)
