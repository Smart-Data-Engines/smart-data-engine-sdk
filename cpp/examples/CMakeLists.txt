cmake_minimum_required(VERSION 3.20)
project(sde_examples LANGUAGES CXX)

# Inside the library's own build the target exists already. Built on its own, this directory is a
# client of an installed library - what `find_package(sde)` gives somebody else's build - and CI
# builds it that way, so the installed package is tested rather than assumed.
if(NOT TARGET sde::sde)
  set(CMAKE_CXX_STANDARD 20)
  set(CMAKE_CXX_STANDARD_REQUIRED ON)
  find_package(sde CONFIG REQUIRED)
endif()

add_executable(sde_example_route route.cpp)
target_link_libraries(sde_example_route PRIVATE sde::sde)

# The trading application of examples/trading, in C++: all three adapters, so it is built when the
# library has them - in this build, or installed with them. Its UUIDs are version 5, from OpenSSL's
# SHA-1, as a client's would be.
if(TARGET sde::postgres AND TARGET sde::clickhouse AND TARGET sde::orderbook)
  if(NOT TARGET OpenSSL::Crypto)
    find_package(OpenSSL 3.0 REQUIRED COMPONENTS Crypto)
  endif()
  add_executable(sde_example_trading trading/trading.cpp)
  target_link_libraries(sde_example_trading PRIVATE
    sde::sde sde::postgres sde::clickhouse sde::orderbook OpenSSL::Crypto)
  # In this build, the library's own warnings as errors and its sanitizers: the example is code a
  # client copies, held to the library's standard.
  if(TARGET sde_build_flags)
    target_link_libraries(sde_example_trading PRIVATE sde_build_flags)
  endif()
endif()
