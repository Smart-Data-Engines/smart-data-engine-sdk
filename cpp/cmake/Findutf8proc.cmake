# Finds utf8proc and provides the imported target utf8proc::utf8proc.
#
# utf8proc is the one dependency the format contract forces on a C++ library: NFC normalisation
# (docs/format-contract.md section 1) is not in the C++ standard library. Upstream installs a CMake
# package when it is built with CMake; distributions that build it with its Makefile (Debian and
# Ubuntu among them) ship only a pkg-config file. So: the upstream package first, then pkg-config,
# then a plain search for the header and the library.

if(TARGET utf8proc::utf8proc)
  set(utf8proc_FOUND TRUE)
  return()
endif()

find_package(utf8proc CONFIG QUIET)
if(utf8proc_FOUND)
  if(TARGET utf8proc AND NOT TARGET utf8proc::utf8proc)
    add_library(utf8proc::utf8proc ALIAS utf8proc)
  endif()
  if(TARGET utf8proc::utf8proc)
    return()
  endif()
endif()

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(PC_UTF8PROC QUIET libutf8proc)
endif()

find_path(utf8proc_INCLUDE_DIR NAMES utf8proc.h HINTS ${PC_UTF8PROC_INCLUDE_DIRS})
find_library(utf8proc_LIBRARY NAMES utf8proc HINTS ${PC_UTF8PROC_LIBRARY_DIRS})

if(utf8proc_INCLUDE_DIR AND EXISTS "${utf8proc_INCLUDE_DIR}/utf8proc.h")
  file(STRINGS "${utf8proc_INCLUDE_DIR}/utf8proc.h" _utf8proc_version_lines
       REGEX "#define UTF8PROC_VERSION_(MAJOR|MINOR|PATCH) ")
  foreach(_line IN LISTS _utf8proc_version_lines)
    if(_line MATCHES "#define UTF8PROC_VERSION_(MAJOR|MINOR|PATCH) ([0-9]+)")
      set(_utf8proc_${CMAKE_MATCH_1} "${CMAKE_MATCH_2}")
    endif()
  endforeach()
  if(DEFINED _utf8proc_MAJOR)
    set(utf8proc_VERSION "${_utf8proc_MAJOR}.${_utf8proc_MINOR}.${_utf8proc_PATCH}")
  endif()
endif()

include(FindPackageHandleStandardArgs)
find_package_handle_standard_args(utf8proc
  REQUIRED_VARS utf8proc_LIBRARY utf8proc_INCLUDE_DIR
  VERSION_VAR utf8proc_VERSION)

if(utf8proc_FOUND AND NOT TARGET utf8proc::utf8proc)
  add_library(utf8proc::utf8proc UNKNOWN IMPORTED)
  set_target_properties(utf8proc::utf8proc PROPERTIES
    IMPORTED_LOCATION "${utf8proc_LIBRARY}"
    INTERFACE_INCLUDE_DIRECTORIES "${utf8proc_INCLUDE_DIR}")
endif()

mark_as_advanced(utf8proc_INCLUDE_DIR utf8proc_LIBRARY)
