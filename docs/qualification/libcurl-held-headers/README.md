# A header libcurl holds until the next line, and the bound on silence

The C++ library's ClickHouse adapter gives up on an exchange when the server has been silent for
`send_receive_timeout`, and a long statement is kept alive by the progress headers the server
sends while it works. "Silent" used to mean that neither the header callback nor the body
callback had been called. This record is why it means that nothing happened on the socket now.

Measured on 15 November 2026 on one laptop. It began with a test of the suite that failed once in
a full run and then 2 times in 30 run alone.

## What libcurl does with a header line

[`probe_paced_server.py`](probe_paced_server.py) answers a request with a status line, then one
`X-ClickHouse-Progress` header a second for four seconds, then the end of the headers and a body.
The `curl` tool of each release fetched it with `-v --trace-time`, which prints a header line when
libcurl hands it on. When each of the four progress headers was handed on, in seconds after the
status line:

| libcurl | sent at 1.0 | sent at 2.0 | sent at 3.0 | sent at 4.0 | |
|---|---|---|---|---|---|
| 8.5.0, 8.11.1, 8.14.1, 8.16.0, 8.17.0 | 1.0 | 2.0 | 3.0 | 4.0 | when it arrives |
| 8.18.0, 8.23.0 | **2.0** | **3.0** | **4.0** | **4.5** | when the next line begins |

8.18.0 was measured twice, as the curl project's own image and as Ubuntu's package
`8.18.0-1ubuntu2.8`, with the same result; 8.23.0 was the newest release that day. The last header
is handed on with the end of the headers, at 4.5 s. A header line can be continued by the line
after it, and from 8.18.0 libcurl looks at how that line begins before it calls a header complete.
8.5.0 is what Ubuntu 24.04 has, and so what this repository's CI runs.

Results: [`paced-8.5.0.txt`](paced-8.5.0.txt), [`paced-8.11.1.txt`](paced-8.11.1.txt),
[`paced-8.14.1.txt`](paced-8.14.1.txt), [`paced-8.16.0.txt`](paced-8.16.0.txt),
[`paced-8.17.0.txt`](paced-8.17.0.txt), [`paced-8.18.0.txt`](paced-8.18.0.txt),
[`paced-8.18.0-ubuntu.txt`](paced-8.18.0-ubuntu.txt), [`paced-8.23.0.txt`](paced-8.23.0.txt).

## Against ClickHouse itself

A query that sleeps a second a row for five rows, with a progress header asked for every second,
against ClickHouse 24.8.14.39
([`clickhouse-24.8-through-8.17.0.txt`](clickhouse-24.8-through-8.17.0.txt),
[`clickhouse-24.8-through-8.18.0.txt`](clickhouse-24.8-through-8.18.0.txt)):

| | status line | first progress header | then |
|---|---|---|---|
| through 8.17.0 | 1.003 s | 1.004 s | 2.0, 3.0, 4.0, 5.0 s |
| through 8.18.0 | 1.003 s | **2.0 s** | 3.0, 4.0, 5.0, 5.0 s |

The server sends its status line with its first progress header, not before it. So through
8.18.0 each progress header reaches the application an interval late, and still something reaches
it every interval: the status line at the first, and after that the header before the one that
just arrived. **Against the server itself the earlier bound was not seen to misfire**, and by this
arithmetic it would not: the callbacks keep the pace of the arrivals.

## Where the earlier bound did misfire

Where a header arrives alone and a pause follows it. It was not heard when it arrived, so the
bound went on counting from the callback before it.

- **The suite's own test of the keep-alive is that shape**: a status line at once, then a progress
  header a second, under a bound of two seconds. On libcurl 8.18.0 the first callback after the
  status line came two seconds after it - at the bound exactly, so the test passed or failed by
  which of the two happened first. Run alone it failed 2 times in 30, and 4 in 40 under `strace`:
  in each of those four the adapter closed its connection 2.00 to 2.01 s after the request, 1.00 s
  after a progress header had arrived. On 8.5.0 the header is heard at one second, which is why CI
  never showed it.
- **A header every second and a half under a two-second bound** arrives in time every time, and
  the adapter as it was gave up on the answer: its first callback after the status line was due
  at three seconds, a second past the bound.
- **A header at one second and then nothing** was given up on two seconds after the request, one
  second after the header, where the bound is on silence since the header.

The last two are tests of this change;
[`earlier-adapter-on-8.18.0.txt`](earlier-adapter-on-8.18.0.txt) is the adapter as it was, built
against 8.18.0, failing both.

## What changed

`send_receive_timeout` is documented as socket inactivity, and the reference's driver bounds its
socket. The adapter now does the same: it runs the transfer itself and counts silence from the
last time anything arrived on the socket or left it, whatever libcurl has handed on by then.
After the change the test that failed 2 times in 30 passed 40 times in 40, and the two tests
above pass, on libcurl 8.18.0.

What this does not show: a libcurl of 8.18.0 or later in CI. The runners have 8.5.0, where the
earlier adapter and this one behave alike, so these tests can fail for the earlier adapter only
where the library is newer, as on the laptop that found it.
