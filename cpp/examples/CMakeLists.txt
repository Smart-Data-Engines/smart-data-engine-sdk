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
