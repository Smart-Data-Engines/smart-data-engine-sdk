cmake_minimum_required(VERSION 3.20)
project(sde_soak_worker LANGUAGES CXX)

# The soak's C++ application process (python/tests/qualification/soak.py), built as a client of an
# installed library - what find_package(sde) gives somebody else's build - and never inside the
# library's own build: the soak measures the library a client links, not its test build.
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(sde CONFIG REQUIRED)

add_executable(sde_soak_worker soak_worker.cpp)
target_link_libraries(sde_soak_worker PRIVATE sde::sde sde::postgres sde::clickhouse)
install(TARGETS sde_soak_worker RUNTIME DESTINATION bin)
