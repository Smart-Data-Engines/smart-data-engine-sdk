# Requirement 6.1: the core opens no socket and links no network library. Read from the archive
# itself - the symbols it leaves for the linker to find (`nm -u`) - rather than from the build files,
# which say what is linked and not what the code calls. `EXPECT` is `none` for the core; for an
# adapter's archive it is the library that adapter is built on, which must be found, so the check
# cannot pass by reading nothing.
#
#   cmake -DNM=nm -DARCHIVE=libsde.a -DEXPECT=none|sockets|libpq|libcurl -P core_symbols.cmake

foreach(required NM ARCHIVE EXPECT)
  if(NOT DEFINED ${required})
    message(FATAL_ERROR "${required} is required")
  endif()
endforeach()

execute_process(COMMAND "${NM}" -u "${ARCHIVE}"
  OUTPUT_VARIABLE listing ERROR_VARIABLE errors RESULT_VARIABLE status)
if(NOT status EQUAL 0)
  message(FATAL_ERROR "${NM} could not read ${ARCHIVE}: ${errors}")
endif()

set(sockets "socket|connect|getaddrinfo|freeaddrinfo|gethostbyname|gethostbyname_r|send|recv|sendto|recvfrom|sendmsg|recvmsg|bind|listen|accept|accept4|poll|ppoll|select|epoll_create1|epoll_ctl|epoll_wait|SSL_[A-Za-z0-9_]+|TLS_[A-Za-z0-9_]+|BIO_new_socket")
set(libpq "PQ[A-Za-z0-9_]+")
set(libcurl "curl_[a-z0-9_]+")

# One undefined symbol per line, `U name`, possibly indented, in every object of the archive.
string(REPLACE "\n" ";" lines "${listing}")
set(found "")
foreach(line IN LISTS lines)
  if(line MATCHES "^[ \t]*U[ \t]+([^ \t@]+)")
    set(symbol "${CMAKE_MATCH_1}")
    foreach(family sockets libpq libcurl)
      if(symbol MATCHES "^(${${family}})$")
        list(APPEND found "${family}:${symbol}")
      endif()
    endforeach()
  endif()
endforeach()
list(REMOVE_DUPLICATES found)

if(EXPECT STREQUAL "none")
  if(found)
    message(FATAL_ERROR "the core calls into a network library: ${found}. Tier 0 and Tier 1 open no "
                        "socket, and the core is what an application linking no adapter carries.")
  endif()
  message(STATUS "${ARCHIVE}: no network symbol")
else()
  list(FILTER found INCLUDE REGEX "^${EXPECT}:")
  if(NOT found)
    message(FATAL_ERROR "${ARCHIVE} shows no ${EXPECT} symbol, so this check reads nothing it "
                        "should: the control failed, and with it the core's result")
  endif()
  list(LENGTH found count)
  message(STATUS "${ARCHIVE}: ${count} ${EXPECT} symbols, as its adapter needs")
endif()
