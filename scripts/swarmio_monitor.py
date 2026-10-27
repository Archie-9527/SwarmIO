#!/usr/bin/env python3
"""BaM/SwarmIO 资源与完成吞吐采样；仅用 Python 标准库，生成 CSV、SVG 和 HTML。

由 swarmio_bam.py run --monitor-interval 调用；本文件也可用于重新生成历史 CSV 的图表。
GPU 通过一个常驻 nvidia-smi 子进程采样，绝不向 benchmark 或内核线程发送信号。
"""
import argparse
import contextlib
import csv
import datetime as dt
import html
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import threading
import time

PROC = Path('/proc')
METRICS = Path('/sys/module/swarmio/parameters/io_metrics')
LATENCY_SWITCH = Path('/sys/module/swarmio/parameters/io_latency')
BAR_COUNTERS = {'bar_ios_total': 'latency_ios', 'bar_segments_total': 'segments',
                'bar_map_ns_total': 'map_ns', 'bar_copy_ns_total': 'copy_ns',
                'bar_unmap_ns_total': 'unmap_ns', 'bar_span_ns_total': 'bar_ns',
                'bar_xfer_ns_total': 'xfer_ns', 'bar_request_ns_total': 'request_ns'}
BAR_AVERAGES = {'bar_map_us': 'bar_map_ns_total', 'bar_copy_us': 'bar_copy_ns_total',
                'bar_unmap_us': 'bar_unmap_ns_total', 'bar_span_us': 'bar_span_ns_total',
                'bar_xfer_us': 'bar_xfer_ns_total', 'bar_request_us': 'bar_request_ns_total'}
GPU_FIELDS = ('gpu_busy_pct', 'gpu_memory_busy_pct', 'gpu_memory_used_mib',
              'gpu_memory_total_mib', 'gpu_power_w', 'gpu_temperature_c')
FIELDS = ('time', 'elapsed_s', 'sample_duration_s', 'gpu_age_s', *GPU_FIELDS,
          'host_cpu_busy_pct', 'host_cpu_iowait_pct', 'host_memory_used_pct',
          'host_memory_used_mib', 'host_memory_available_mib', 'host_swap_used_mib',
          'bam_cpu_pct', 'bam_rss_mib', 'swarmio_cpu_pct', 'monitor_cpu_pct',
          'nvidia_smi_cpu_pct', 'nvme_ops_total', 'nvme_bytes_total', 'nvme_errors_total',
          'nvme_sample_ns', 'nvme_iops', 'nvme_miops', 'nvme_bandwidth_gib_s',
          *BAR_COUNTERS, *BAR_AVERAGES, 'bar_calls_us', 'bar_calls_share_xfer_pct',
          'bar_calls_share_request_pct')
NOTES = [
    'GPU 指标为所选 GPU 整卡数据；显存占用不等于 BaM 独占用量。',
    'GPU busy 为采样窗口内执行 kernel 的时间比例，包含轮询；不是 SM occupancy 或算术单元利用率。',
    'GPU memory busy 是显存读写忙碌时间比例，memory used 是已占用 MiB，两者含义不同。',
    'nvidia-smi 的驱动刷新窗口可能比打点间隔长；重复值并不表示采样失效。',
    'host CPU busy 按整机归一到 100%；进程/SwarmIO CPU 以一个核为 100%，多核可超过 100%。',
    'SwarmIO 轮询线程即使没有 I/O 也可能占用 CPU。主机 RAM 不包含 memmap 预留的仿真 SSD 内存。',
    'NVMe 曲线为全部队列成功读写 CQE 数与真实命令字节数的差分，包含 BaM 额外读；不是逻辑 IOPS。',
    '完成计数表示 CPU 已发布 CQE，不证明 GPU 已观察到它；各队列顺序聚合存在微小采样偏差。',
    '资源图从 benchmark 进程启动附近开始，包含初始化与清理；BaM 平均吞吐只覆盖其 kernel 计时区间。',
    '采样自身会产生开销。此工具不测 PCIe 链路吞吐、DRAM 带宽或 GPU warp/SM occupancy。',
    'BAR 延迟仅统计启用打点且成功完成的 GPU 数据命令，包含 BaM 额外读；不保存逐请求时间序列。',
    'map/copy/unmap 为 CPU 调用耗时（含可能的抢占与等待），不是 PCIe 传输到达 GPU 的完成延迟；未增加读回或 flush。',
    'BAR span 是各数据段从 ioremap 开始至 iounmap 返回的累计时间；不含 SQ/CQ 常驻映射和 PRP 列表映射。',
    'CPU xfer 是整个 CPU 数据搬运函数耗时；host request 是 dispatcher 开始复制 SQE 至 CPU 发布 CQE，含排队和仿真延迟，不含 GPU 端完整请求生命周期。',
    '占比按同一批请求的累计耗时相除；累计阶段耗时/整轮墙钟时间可超过 100%，不能当成可相加的墙钟时间占比。',
]


def interval_value(value):
    result = float(value)
    if not math.isfinite(result) or not 0.01 <= result <= 60:
        raise argparse.ArgumentTypeError('打点间隔必须为 0.01..60 秒，例如 0.1')
    return result


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def cpu_stats(text):
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r'cpu\d*', parts[0]):
            # guest/guest_nice 已计入 user/nice，不能再次加总。
            values = list(map(int, parts[1:9]))
            result[parts[0]] = (sum(values), values[3], values[4])
    return result


def cpu_delta(previous, current):
    total = current[0] - previous[0]
    if total <= 0:
        return None, None
    idle = current[1] - previous[1]
    wait = current[2] - previous[2]
    return 100 * (total - idle - wait) / total, 100 * wait / total


def task_stat(text):
    # comm 可以含空格甚至右括号，不能对整行直接 split。
    end = text.rfind(')')
    fields = text[end + 2:].split()
    return (int(fields[19]), int(fields[11]) + int(fields[12]), int(fields[21]))


def io_stats(text):
    values = {key: int(value) for key, value in re.findall(r'(\w+)=(\d+)', text)}
    if values.get('version') not in (1, 2) or not all(k in values for k in ('sample_ns', 'ops', 'bytes', 'errors')):
        raise ValueError('不支持的 io_metrics 格式')
    if values['version'] == 2 and not all(key in values for key in BAR_COUNTERS.values()):
        raise ValueError('io_metrics v2 缺少延迟计数')
    return values


@contextlib.contextmanager
def bar_latency_mode():
    """在启动 benchmark 前开启，离开测试范围后恢复；不清零共享累计计数。"""
    baseline = io_stats(METRICS.read_text())
    if baseline['version'] < 2:
        raise ValueError('BAR 延迟需要 io_metrics v2，请重新编译并加载 SwarmIO')
    previous = LATENCY_SWITCH.read_text().strip()
    LATENCY_SWITCH.write_text('Y\n')
    state = {'previous': previous, 'restored': False, 'baseline': baseline}
    try:
        yield state
    finally:
        try:
            state['final'] = io_stats(METRICS.read_text())
        except (OSError, ValueError) as exc:
            state['counter_warning'] = str(exc)
        try:
            LATENCY_SWITCH.write_text(previous + '\n')
            state['restored'] = True
        except OSError as exc:
            print('WARNING: 无法恢复 io_latency 开关：' + str(exc), file=sys.stderr)


def bar_interval(previous, current):
    if previous.get('version') != 2 or current.get('version') != 2:
        return {}
    delta = {field: current[key] - previous[key] for field, key in BAR_COUNTERS.items()}
    if any(value < 0 for value in delta.values()) or delta['bar_ios_total'] <= 0:
        return {}
    result = {field: delta[counter] / delta['bar_ios_total'] / 1000
              for field, counter in BAR_AVERAGES.items()}
    calls = delta['bar_map_ns_total'] + delta['bar_copy_ns_total'] + delta['bar_unmap_ns_total']
    result['bar_calls_us'] = calls / delta['bar_ios_total'] / 1000
    for field, denominator in (('bar_calls_share_xfer_pct', 'bar_xfer_ns_total'),
                               ('bar_calls_share_request_pct', 'bar_request_ns_total')):
        if delta[denominator] > 0:
            result[field] = 100 * calls / delta[denominator]
    return result


def io_delta(previous, current):
    elapsed = (current['sample_ns'] - previous['sample_ns']) / 1e9
    ops = current['ops'] - previous['ops']
    size = current['bytes'] - previous['bytes']
    if elapsed <= 0 or ops < 0 or size < 0:
        return None, None
    return ops / elapsed, size / elapsed / (1024 ** 3)


class ResourceMonitor:
    def __init__(self, directory, gpu_uuid, pid, interval):
        self.directory = Path(directory)
        self.gpu_uuid, self.pid = gpu_uuid, pid
        self.interval = interval_value(interval)
        self.stop_event = threading.Event()
        self.warnings = []
        self.gpu = None
        self.gpu_values, self.gpu_time = {}, None
        self.gpu_buffer = b''
        self.previous_cpu, self.previous_tasks, self.previous_io = {}, {}, None
        self.tasks, self.next_discovery = {}, 0
        self.hz = os.sysconf('SC_CLK_TCK')
        self.page_bytes = os.sysconf('SC_PAGE_SIZE')
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, name='swarmio-monitor', daemon=True)

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.warn('采样线程未及时退出；CSV 可能仍在写入，当前图表仅覆盖已刷新数据。')

    def _gpu_start(self):
        self.gpu_log = (self.directory / 'gpu-sampler.log').open('wb')
        try:
            self.gpu = subprocess.Popen([
                'nvidia-smi', '--id=' + self.gpu_uuid,
                '--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,temperature.gpu',
                '--format=csv,noheader,nounits', '--loop-ms=' + str(round(self.interval * 1000))],
                stdout=subprocess.PIPE, stderr=self.gpu_log, start_new_session=True)
            os.set_blocking(self.gpu.stdout.fileno(), False)
        except OSError as exc:
            self.warn('无法启动 GPU 采样：' + str(exc))

    def _gpu_read(self, now):
        if not self.gpu:
            return
        # 非阻塞读取，GPU 驱动异常不能阻塞主机打点或 benchmark 的退出处理。
        try:
            for _ in range(16):
                block = os.read(self.gpu.stdout.fileno(), 4096)
                if not block:
                    break
                self.gpu_buffer += block
        except BlockingIOError:
            pass
        while b'\n' in self.gpu_buffer:
            line, self.gpu_buffer = self.gpu_buffer.split(b'\n', 1)
            parts = next(csv.reader([line.decode(errors='replace')]))
            if len(parts) != len(GPU_FIELDS):
                self.warn('GPU 输出包含非指标行，详见 gpu-raw.csv 或 gpu-sampler.log。')
                continue
            values = dict(zip(GPU_FIELDS, map(number, parts)))
            if all(v is None for v in values.values()):
                self.warn('GPU 指标不可用；缺失值保持为空，不按 0 处理。')
            self.gpu_values, self.gpu_time = values, now
            self.gpu_writer.writerow({'elapsed_s': now - self.started, **values})
        if self.gpu.poll() is not None:
            self.warn('nvidia-smi 已退出；请检查 gpu-sampler.log。')

    def _discover_tasks(self, now):
        if now < self.next_discovery:
            return
        self.next_discovery = now + 1
        self.tasks = {self.pid: 'bam', os.getpid(): 'monitor'}
        if self.gpu:
            self.tasks[self.gpu.pid] = 'nvidia-smi'
        for path in PROC.iterdir():
            if path.name.isdigit():
                try:
                    if (path / 'comm').read_text().strip().startswith('swarmio_'):
                        self.tasks[int(path.name)] = 'swarmio'
                except OSError:
                    pass

    def _sample(self):
        begin = time.monotonic()
        row = {'time': dt.datetime.now().astimezone().isoformat(), 'elapsed_s': begin - self.started}
        self._gpu_read(begin)
        # 超过两个间隔或 1 秒未收到新数据时留空，避免把旧数据无限延长。
        if self.gpu_time is not None:
            row['gpu_age_s'] = begin - self.gpu_time
            if row['gpu_age_s'] <= max(1, 2 * self.interval):
                row.update(self.gpu_values)
        current_cpu = cpu_stats((PROC / 'stat').read_text())
        for cpu, counters in current_cpu.items():
            if cpu in self.previous_cpu:
                busy, wait = cpu_delta(self.previous_cpu[cpu], counters)
                self.cpu_writer.writerow({'elapsed_s': row['elapsed_s'], 'cpu': cpu,
                                          'busy_pct': busy, 'iowait_pct': wait})
                if cpu == 'cpu':
                    row.update(host_cpu_busy_pct=busy, host_cpu_iowait_pct=wait)
        self.previous_cpu = current_cpu
        mem = {key: int(value) for key, value in re.findall(r'^(\w+):\s+(\d+)',
                    (PROC / 'meminfo').read_text(), re.M)}
        used = mem['MemTotal'] - mem['MemAvailable']
        row.update(host_memory_used_pct=100 * used / mem['MemTotal'], host_memory_used_mib=used / 1024,
                   host_memory_available_mib=mem['MemAvailable'] / 1024,
                   host_swap_used_mib=(mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)) / 1024)
        self._discover_tasks(begin)
        sums, available = {}, set()
        for pid, role in self.tasks.items():
            try:
                text = (PROC / str(pid) / 'stat').read_text()
                identity, ticks, rss = task_stat(text)
            except (OSError, ValueError, IndexError):
                continue
            comm = text[text.find('(') + 1:text.rfind(')')]
            previous = self.previous_tasks.get(pid)
            usage = None
            if previous and previous[0] == identity and begin > previous[2]:
                usage = 100 * (ticks - previous[1]) / self.hz / (begin - previous[2])
                sums[role] = sums.get(role, 0) + usage
                available.add(role)
            self.previous_tasks[pid] = (identity, ticks, begin)
            self.task_writer.writerow({'elapsed_s': row['elapsed_s'], 'pid': pid, 'role': role,
                                       'comm': comm, 'cpu_pct': usage, 'rss_mib': rss * self.page_bytes / 2**20})
            if role == 'bam':
                row['bam_rss_mib'] = rss * self.page_bytes / 2**20
        for role, field in (('bam', 'bam_cpu_pct'), ('swarmio', 'swarmio_cpu_pct'),
                            ('monitor', 'monitor_cpu_pct'), ('nvidia-smi', 'nvidia_smi_cpu_pct')):
            if role in available:
                row[field] = sums[role]
        try:
            current_io = io_stats(METRICS.read_text())
            row.update(nvme_ops_total=current_io['ops'], nvme_bytes_total=current_io['bytes'],
                       nvme_errors_total=current_io['errors'], nvme_sample_ns=current_io['sample_ns'])
            if current_io['version'] == 2:
                row.update({field: current_io[key] for field, key in BAR_COUNTERS.items()})
            if self.previous_io:
                row['nvme_iops'], row['nvme_bandwidth_gib_s'] = io_delta(self.previous_io, current_io)
                if row['nvme_iops'] is not None:
                    row['nvme_miops'] = row['nvme_iops'] / 1e6
                row.update(bar_interval(self.previous_io, current_io))
            self.previous_io = current_io
        except (OSError, ValueError) as exc:
            self.warn('无可用的全队列 io_metrics，NVMe 曲线留空；需重编译并加载新增计数的模块。' +
                      ' 原因：' + str(exc))
        row['sample_duration_s'] = time.monotonic() - begin
        self.writer.writerow(row)
        for stream in self.streams:
            stream.flush()

    def _run(self):
        self.streams = []
        try:
            for name, fields, attribute in (
                ('resources.csv', FIELDS, 'writer'),
                ('cpu-cores.csv', ('elapsed_s', 'cpu', 'busy_pct', 'iowait_pct'), 'cpu_writer'),
                ('tasks.csv', ('elapsed_s', 'pid', 'role', 'comm', 'cpu_pct', 'rss_mib'), 'task_writer'),
                ('gpu-raw.csv', ('elapsed_s', *GPU_FIELDS), 'gpu_writer')):
                stream = (self.directory / name).open('w', newline='')
                self.streams.append(stream)
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                setattr(self, attribute, writer)
            self._gpu_start()
            deadline = time.monotonic()
            while True:
                self._sample()
                if self.stop_event.is_set():
                    break
                deadline += self.interval
                # 不补采已经错过的点；真实时间戳及采样耗时保留在 CSV。
                if deadline < time.monotonic():
                    deadline = time.monotonic() + self.interval
                if self.stop_event.wait(max(0, deadline - time.monotonic())):
                    self._sample()
                    break
        except Exception as exc:
            self.warn('采样异常（未中断 I/O）：' + repr(exc))
        finally:
            if self.gpu:
                try:
                    self.gpu.terminate()
                    self.gpu.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    # 仅回收本工具创建的监控子进程，绝不操作 benchmark。
                    self.gpu.kill()
                    try:
                        self.gpu.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        self.warn('GPU 监控子进程仍未退出，PID=' + str(self.gpu.pid))
                except OSError:
                    pass
                self.gpu.stdout.close()
            if hasattr(self, 'gpu_log'):
                self.gpu_log.close()
            for stream in self.streams:
                stream.close()

    def report(self, record):
        return render(self.directory, {'interval_s': self.interval, 'gpu_uuid': self.gpu_uuid,
                                      'warnings': self.warnings, 'run_state': record.get('state'),
                                      'bar_latency_requested': record.get('bar_latency_requested', False),
                                      'bar_latency_boundary': record.get('bar_latency_setting')})


COLORS = ('#2563eb', '#dc2626', '#059669', '#9333ea', '#d97706', '#0891b2')


def plain_number(value):
    """保留约四位有效数字，但永不使用科学计数法；仅用于展示。"""
    if value is None or not math.isfinite(value):
        return '—'
    places = max(0, 3 - math.floor(math.log10(abs(value)))) if value else 0
    text = format(value, f',.{places}f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def bar_summary(rows, metadata):
    boundary = metadata.get('bar_latency_boundary') or {}
    if boundary.get('baseline') and boundary.get('final'):
        first = {field: boundary['baseline'][key] for field, key in BAR_COUNTERS.items()}
        last = {field: boundary['final'][key] for field, key in BAR_COUNTERS.items()}
        scope = 'benchmark 进程启动前至运行退出/中断后的计数边界；未完成请求不在成功统计中'
    else:
        valid = [row for row in rows if all(number(row.get(key)) is not None for key in BAR_COUNTERS)]
        if len(valid) < 2:
            return None
        first, last = valid[0], valid[-1]
        scope = '首末有效资源采样点之间；不保证覆盖整个进程'
    delta = {key: int(last[key]) - int(first[key]) for key in BAR_COUNTERS}
    if any(value < 0 for value in delta.values()) or delta['bar_ios_total'] <= 0:
        return None
    ios, xfer, request = delta['bar_ios_total'], delta['bar_xfer_ns_total'], delta['bar_request_ns_total']
    calls = sum(delta[key] for key in ('bar_map_ns_total', 'bar_copy_ns_total', 'bar_unmap_ns_total'))
    stage_times = [('ioremap', delta['bar_map_ns_total']), ('memcpy_toio/fromio', delta['bar_copy_ns_total']),
                   ('iounmap', delta['bar_unmap_ns_total']), ('three_calls_sum', calls),
                   ('bar_span', delta['bar_span_ns_total']), ('cpu_xfer', xfer), ('host_request', request)]
    stages = {name: {'total_ns': ns, 'total_ms': ns / 1e6, 'avg_us_per_io': ns / ios / 1000,
                     'share_of_cpu_xfer_pct': 100 * ns / xfer if xfer else None,
                     'share_of_host_request_pct': 100 * ns / request if request else None}
              for name, ns in stage_times}
    result = {'scope': scope, 'successful_bar_ios': ios, 'data_segments': delta['bar_segments_total'], 'stages': stages}
    wall = metadata.get('bam_kernel_average', {}).get('elapsed_s')
    if wall and wall > 0:
        result['cumulative_calls_over_bam_wall_pct'] = 100 * calls / (wall * 1e9)
        result['cumulative_bar_span_over_bam_wall_pct'] = 100 * delta['bar_span_ns_total'] / (wall * 1e9)
        result['wall_ratio_note'] = '多 worker 累计时间/整轮墙钟时间，允许超过 100%；不是串行墙钟占比。'
    return result


def bar_table(summary):
    if not summary:
        return '<h2>GPU BAR 延迟</h2><p>无有效延迟数据。启用 --bar-latency 并加载 io_metrics v2 模块后重新测试；历史数据无法补算。</p>'
    labels = {'ioremap': 'ioremap', 'memcpy_toio/fromio': 'memcpy_toio / memcpy_fromio', 'iounmap': 'iounmap',
              'three_calls_sum': '三段调用合计', 'bar_span': 'BAR 整段（含段内其他处理）',
              'cpu_xfer': '整个 CPU 数据搬运函数', 'host_request': '主机侧请求（含排队和仿真延迟）'}
    out = '<h2>GPU BAR 延迟汇总</h2><p>' + html.escape(summary['scope']) + '</p>'
    out += '<p>成功 BAR 命令数：' + plain_number(summary['successful_bar_ios'])
    out += '；数据段数：' + plain_number(summary['data_segments']) + '。平均值按命令计，包含该命令的所有数据段。</p>'
    out += '<table><thead><tr><th>阶段</th><th>累计耗时 ms</th><th>平均 μs/命令</th><th>占 CPU 搬运函数 %</th><th>占主机请求延迟 %</th></tr></thead><tbody>'
    for name, values in summary['stages'].items():
        # 整个请求包含排队，不放到 CPU 搬运函数内部占比列，避免误读。
        fraction = None if name == 'host_request' else values['share_of_cpu_xfer_pct']
        out += '<tr><td>' + labels[name] + '</td>'
        for value in (values['total_ms'], values['avg_us_per_io'], fraction, values['share_of_host_request_pct']):
            out += '<td>' + plain_number(value) + '</td>'
        out += '</tr>'
    out += '</tbody></table>'
    if 'cumulative_calls_over_bam_wall_pct' in summary:
        out += '<p>三段调用累计耗时 / BaM 整轮墙钟时间：' + plain_number(summary['cumulative_calls_over_bam_wall_pct'])
        out += '%。' + html.escape(summary['wall_ratio_note']) + '</p>'
    return out


def chart(rows, title, series, unit):
    width, height, left, top, pw, ph = 960, 300, 82, 50, 850, 195
    xmax = max([number(r.get('elapsed_s')) or 0 for r in rows] + [0.001])
    values = [number(r.get(key)) for r in rows for key, _ in series]
    valid = [v for v in values if v is not None]
    ymax = (max(valid) if valid and max(valid) > 0 else 1) * 1.08
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<g font-family="sans-serif" font-size="12" fill="#334155">',
           f'<text x="{left}" y="22" font-size="16">{html.escape(title)}</text>']
    for i in range(5):
        y = top + ph * i / 4
        out += [f'<path d="M {left} {y} H {left+pw}" stroke="#e2e8f0"/>',
                f'<text x="{left-8}" y="{y+4}" text-anchor="end">{plain_number(ymax*(1-i/4))}</text>']
        x = left + pw * i / 4
        out.append(f'<text x="{x}" y="{top+ph+18}" text-anchor="middle">{plain_number(xmax*i/4)}</text>')
    out += [f'<text x="{left}" y="{top-10}">{html.escape(unit)}</text>',
            f'<text x="{left+pw}" y="{top+ph+37}" text-anchor="end">Elapsed seconds</text>']
    for index, (key, label) in enumerate(series):
        color = COLORS[index % len(COLORS)]
        # 图表最多保留约 4000 点；原始 CSV 不做降采样。
        step = max(1, math.ceil(len(rows) / 4000))
        points = []
        for i in range(0, len(rows), step):
            value = number(rows[i].get(key))
            x = left + pw * (number(rows[i].get('elapsed_s')) or 0) / xmax
            if value is None:
                if points:
                    out.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{" ".join(points)}"/>')
                    points = []
                continue
            y = top + ph * (1 - value / ymax)
            points.append(f'{x:.2f},{y:.2f}')
            if len(rows) < 3:
                out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}"/>')
        if points:
            out.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{" ".join(points)}"/>')
        out.append(f'<text x="{left+index*175}" y="{height-8}" fill="{color}">{html.escape(label)}</text>')
    if not valid:
        out.append('<text x="500" y="145" text-anchor="middle">No available samples</text>')
    return ''.join(out) + '</g></svg>'


def render(directory, metadata=None):
    directory = Path(directory)
    old = directory / 'monitor-summary.json'
    if metadata is None:
        metadata = json.loads(old.read_text()) if old.exists() else {}
    path = directory / 'resources.csv'
    rows = []
    if path.exists():
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
    for row in rows:
        iops = number(row.get('nvme_iops'))
        row['nvme_miops'] = iops / 1e6 if iops is not None else None
    bench = (directory / 'bench.log').read_text(errors='replace') if (directory / 'bench.log').exists() else ''
    match = re.search(r'Elapsed Time:\s*([\deE+.-]+)\s+Number of Ops:\s*(\d+)\s+Data Size \(bytes\):\s*(\d+)', bench)
    if match:
        elapsed, ops, size = float(match[1]) / 1e6, int(match[2]), int(match[3])
        metadata['bam_kernel_average'] = {'elapsed_s': elapsed, 'logical_ops': ops, 'logical_bytes': size,
            'logical_iops': ops / elapsed if elapsed > 0 else None,
            'logical_bandwidth_gib_s': size / elapsed / 2**30 if elapsed > 0 else None}
    metadata.update(samples=len(rows), notes=NOTES)
    intervals = [float(b['elapsed_s']) - float(a['elapsed_s']) for a, b in zip(rows, rows[1:])]
    costs = [number(r.get('sample_duration_s')) for r in rows]
    costs = [v for v in costs if v is not None]
    if intervals:
        metadata['actual_interval_s'] = {'min': min(intervals), 'median': statistics.median(intervals),
                                        'max': max(intervals)}
    if costs:
        metadata['sample_cost_s'] = {'mean': statistics.mean(costs), 'max': max(costs)}
    if len(rows) < 3:
        warnings = metadata.setdefault('warnings', [])
        message = '采样少于 3 点，建议增加 reqs 延长测试；单点不能反映时间变化。'
        if message not in warnings:
            warnings.append(message)
    io_rows = [r for r in rows if number(r.get('nvme_ops_total')) is not None]
    if len(io_rows) > 1:
        first, last = io_rows[0], io_rows[-1]
        elapsed = (int(last['nvme_sample_ns']) - int(first['nvme_sample_ns'])) / 1e9
        ops = int(last['nvme_ops_total']) - int(first['nvme_ops_total'])
        size = int(last['nvme_bytes_total']) - int(first['nvme_bytes_total'])
        if elapsed > 0 and ops >= 0 and size >= 0:
            metadata['nvme_monitor_window'] = {'elapsed_s': elapsed, 'ops': ops, 'bytes': size,
                'iops': ops / elapsed, 'bandwidth_gib_s': size / elapsed / 2**30}
    metadata.pop('gpu_bar_latency', None)
    latency = bar_summary(rows, metadata)
    if latency:
        metadata['gpu_bar_latency'] = latency
    panels = [
        ('gpu-utilization', 'GPU activity', [('gpu_busy_pct', 'Kernel busy'), ('gpu_memory_busy_pct', 'Memory busy')], '%'),
        ('gpu-memory', 'GPU device memory', [('gpu_memory_used_mib', 'Used'), ('gpu_memory_total_mib', 'Total')], 'MiB'),
        ('host-cpu', 'Host CPU (whole machine = 100%)', [('host_cpu_busy_pct', 'Busy'), ('host_cpu_iowait_pct', 'I/O wait')], '%'),
        ('tasks-cpu', 'Task CPU (one core = 100%)', [('swarmio_cpu_pct', 'SwarmIO threads'), ('bam_cpu_pct', 'BaM process'),
            ('monitor_cpu_pct', 'Python runner'), ('nvidia_smi_cpu_pct', 'GPU sampler')], '%'),
        ('host-memory', 'Host memory', [('host_memory_used_pct', 'Used / total')], '%'),
        ('bam-memory', 'BaM host resident memory', [('bam_rss_mib', 'RSS')], 'MiB'),
        ('nvme-iops', 'SwarmIO successful NVMe completions (including extra reads)', [('nvme_miops', 'NVMe MIOPS')], 'MIOPS'),
        ('nvme-bandwidth', 'SwarmIO completed command payload', [('nvme_bandwidth_gib_s', 'NVMe bandwidth')], 'GiB/s'),
        ('gpu-power', 'GPU power', [('gpu_power_w', 'Power')], 'W'),
        ('bar-latency', 'GPU BAR mean CPU call latency per completed command',
            [('bar_map_us', 'ioremap'), ('bar_copy_us', 'memcpy_toio/fromio'), ('bar_unmap_us', 'iounmap')], 'us / command'),
        ('bar-latency-share', 'Three calls / CPU transfer function and host request latency',
            [('bar_calls_share_xfer_pct', 'Calls / CPU xfer'), ('bar_calls_share_request_pct', 'Calls / host req')], '%'),
    ]
    images = []
    for name, title, series, unit in panels:
        svg = chart(rows, title, series, unit)
        (directory / (name + '.svg')).write_text(svg)
        images.append('<section>' + svg + '</section>')
    # 单独输出逐核曲线，原始明细同时保留为长表 CSV。
    core_path = directory / 'cpu-cores.csv'
    if core_path.exists():
        with core_path.open() as stream:
            cores = list(csv.DictReader(stream))
        grouped, names = {}, set()
        for r in cores:
            if r['cpu'] == 'cpu':
                continue
            names.add(r['cpu'])
            grouped.setdefault(r['elapsed_s'], {'elapsed_s': r['elapsed_s']})[r['cpu']] = r['busy_pct']
        core_rows = list(grouped.values())
        # 每图最多 4 个 CPU，保持图例可读。
        names = sorted(names, key=lambda s: int(s[3:]))
        for index in range(0, len(names), 4):
            group = names[index:index+4]
            svg = chart(core_rows, 'CPU cores ' + ', '.join(group), [(c, c) for c in group], '%')
            (directory / f'cpu-cores-{index//4}.svg').write_text(svg)
            images.append('<section>' + svg + '</section>')
    (directory / 'monitor-summary.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    title = 'BaM + SwarmIO 资源与吞吐采样'
    content = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + title + '</title>'
    content += '<style>body{font:15px system-ui;margin:24px auto;padding:0 18px;max-width:1100px;background:#f1f5f9;color:#0f172a}section{background:white;margin:18px 0;border-radius:8px}svg{width:100%;height:auto}li{margin:8px 0}pre{white-space:pre-wrap;background:white;padding:16px}table{border-collapse:collapse;width:100%;background:white}th,td{padding:10px;border-bottom:1px solid #e2e8f0;text-align:right}th:first-child,td:first-child{text-align:left}</style>'
    content += '<h1>' + title + '</h1><p>CSV 保留原始采样；空白表示不可用，不代表零。图表可单独保存为 SVG。</p>'
    content += bar_table(latency)
    content += '<pre>' + html.escape(json.dumps({k: v for k, v in metadata.items() if k != 'notes'}, ensure_ascii=False, indent=2)) + '</pre>'
    content += '<ul>' + ''.join('<li>' + html.escape(n) + '</li>' for n in NOTES) + '</ul>'
    content += '<p>指标含义参考 <a href="https://docs.nvidia.com/deploy/nvidia-smi/index.html">NVIDIA nvidia-smi 官方文档</a>。</p>'
    (directory / 'monitor.html').write_text(content + ''.join(images) + '</html>')
    return directory / 'monitor.html'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='含 resources.csv 的单轮 results 目录')
    print(render(parser.parse_args().directory))
