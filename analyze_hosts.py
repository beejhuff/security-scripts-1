#!/usr/bin/env python

"""
analyze_hosts - scans one or more hosts for security misconfigurations

Copyright (C) 2015-2016 Peter Mosmans [Go Forward]
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
"""


from __future__ import print_function
from __future__ import unicode_literals

import argparse
import logging
import os
import Queue
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
import urlparse

try:
    import nmap
except ImportError:
    print('[-] Please install python-nmap, e.g. pip install python-nmap',
          file=sys.stderr)
    sys.exit(-1)
try:
    import requests
    import Wappalyzer
except ImportError:
    print('Please install the requests and Wappalyzer modules, e.g. '
          'pip install -r requirements.txt')


VERSION = '0.19'
ALLPORTS = [25, 80, 443, 465, 993, 995, 8080]
SCRIPTS = """banner,dns-nsid,dns-recursion,http-cisco-anyconnect,\
http-php-version,http-title,http-trace,ntp-info,ntp-monlist,nbstat,\
rdp-enum-encryption,rpcinfo,sip-methods,smb-os-discovery,smb-security-mode,\
smtp-open-relay,ssh2-enum-algos,vnc-info,xmlrpc-methods,xmpp-info"""
UNKNOWN = -1


def analyze_url(url, port, options, logfile):
    """
    Analyze an URL using wappalyzer and execute corresponding scans.
    """
    orig_url = url
    if options['framework']:
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning)
        if not urlparse.urlparse(url).scheme:
            if port == 443:
                url = 'https://{0}:{1}'.format(url, port)
            else:
                url = 'http://{0}:{1}'.format(url, port)
        wappalyzer = Wappalyzer.Wappalyzer.latest()
        try:
            page = requests.get(url, auth=None, proxies={}, verify=False)
            if page.status_code == 200:
                webpage = Wappalyzer.WebPage(url, page.text, page.headers)
                analysis = wappalyzer.analyze(webpage)
                logging.info('%s Analysis of %s: %s', orig_url, url, analysis)
                if 'Drupal' in analysis:
                    do_droopescan(url, 'drupal', options, logfile)
                if 'Joomla' in analysis:
                    do_droopescan(url, 'joomla', options, logfile)
                if 'WordPress' in analysis:
                    do_wpscan(url, options, logfile)
            else:
                logging.debug('Got result %s on %s - cannot analyze that',
                              page.status_code, url)
        except requests.exceptions.ConnectionError as exception:
            logging.error('%s Could not connect to %s (%s)', orig_url, url,
                          exception)


def is_admin():
    """
    Check whether script is executed using root privileges.
    """
    if os.name == 'nt':
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin()
        except ImportError:
            return False
    else:
        return os.geteuid() == 0  # pylint: disable=no-member


def preflight_checks(options):
    """
    Check if all tools are there, and disable tools automatically.
    """
    if options['resume']:
        if not os.path.isfile(options['queuefile']) or \
           not os.stat(options['queuefile']).st_size:
            logging.error('Cannot resume - queuefile %s is empty',
                          options['queuefile'])
            sys.exit(-1)
    else:
        if os.path.isfile(options['queuefile']) and \
           os.stat(options['queuefile']).st_size:
            logging.error('Queuefile {0} already exists.\n'.
                          format(options['queuefile']) +
                          '    Use --resume to resume with previous targets, ' +
                          'or delete file manually')
            sys.exit(-1)
    for basic in ['nmap']:
        options[basic] = True
    if options['udp'] and not is_admin() and not options['dry_run']:
        logging.error('UDP portscan needs root permissions')
    try:
        import requests
        import Wappalyzer
    except ImportError:
        logging.error('Disabling --framework due to missing Python libraries')
        options['framework'] = False
    if options['framework']:
        options['droopescan'] = True
        options['wpscan'] = True
    if options['wpscan'] and not is_admin():
        logging.error('Disabling --wpscan as this option needs root permissions')
        options['wpscan'] = False
    options['timeout'] = options['testssl.sh']
    for tool in ['curl', 'droopescan', 'nikto', 'nmap', 'testssl.sh',
                 'timeout', 'wpscan']:
        if options[tool]:
            logging.debug('Checking whether %s is present... ', tool)
            version = '--version'
            if tool == 'nikto':
                version = '-Version'
            result, stdout, stderr = execute_command([tool, version], options)
            if not result:
                logging.error('FAILED: Could not execute %s, disabling checks (%s)',
                              tool, stderr)
                options[tool] = False
            else:
                logging.debug(stdout)
    if not options['nmap']:
        logging.error('nmap is necessary')
        sys.exit(-1)


def execute_command(cmd, options):
    """
    Execute command.

    Returns result, stdout, stderr.
    """
    stdout = ''
    stderr = ''
    result = False
    if options['dry_run']:
        logging.debug(' '.join(cmd))
        return True, stdout, stderr
    try:
        logging.debug(' '.join(cmd))
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        result = not process.returncode
    except OSError:
        pass
    return result, unicode.replace(stdout.decode('utf-8'), '\r\n', '\n'), \
        unicode.replace(stderr.decode('utf-8'), '\r\n', '\n')


def download_cert(host, port, options, logfile):
    """
    Download an SSL certificate and append it to the logfile.
    """
    if options['sslcert']:
        try:
            cert = ssl.get_server_certificate((host, port))
            append_logs(logfile, options, cert)
        except ssl.SSLError:
            pass


def append_logs(logfile, options, stdout, stderr=None):
    """
    Append text strings to logfile.
    """
    if options['dry_run']:
        return
    try:
        if stdout and len(stdout):
            with open(logfile, 'a+') as open_file:
                open_file.write(compact_strings(stdout, options).encode('utf-8'))
        if stderr and len(stderr):
            with open(logfile, 'a+') as open_file:
                open_file.write(compact_strings(stderr, options))
    except IOError:
        logging.error('FAILED: Could not write to %s', logfile)


def append_file(logfile, options, input_file):
    """
    Append file to logfile, and delete @input_file.
    """
    if options['dry_run']:
        return
    try:
        if os.path.isfile(input_file) and os.stat(input_file).st_size:
            with open(input_file, 'r') as read_file:
                append_logs(logfile, options, read_file.read())
        os.remove(input_file)
    except (IOError, OSError) as exception:
        logging.error('FAILED: Could not read %s (%s)', input_file, exception)


def compact_strings(strings, options):
    """
    Remove as much unnecessary strings as possible.
    """
    # remove ' (OK)'
    # remove ^SF:
    # remove
    if not options['compact']:
        return strings
    return '\n'.join([x for x in strings.splitlines() if x and
                      not x.startswith('#')])


def do_curl(host, port, options, logfile):
    """
    Check for HTTP TRACE method.
    """
    if options['trace']:
        command = ['curl', '-qsIA', "'{0}'".format(options['header']),
                   '--connect-timeout', str(options['timeout']), '-X', 'TRACE',
                   '{0}:{1}'.format(host, port)]
        _result, stdout, stderr = execute_command(command, options)  # pylint: disable=unused-variable
        append_logs(logfile, options, stdout, stderr)


def do_droopescan(url, cms, options, logfile):
    """
    Perform a droopescan of type @cmd
    """
    if options['droopescan']:
        logging.debug('Performing %s droopescan on %s', cms, url)
        command = ['droopescan', 'scan', cms, '--quiet', '--url', url]
        _result, stdout, stderr = execute_command(command, options)  # pylint: disable=unused-variable
        append_logs(logfile, options, stdout, stderr)


def do_nikto(host, port, options, logfile):
    """
    Perform a nikto scan.
    """
    command = ['nikto', '-vhost', '{0}'.format(host), '-maxtime',
               '{0}s'.format(options['maxtime']), '-host',
               '{0}:{1}'.format(host, port)]
    if port == 443:
        command.append('-ssl')
    _result, stdout, stderr = execute_command(command, options)  # pylint: disable=unused-variable
    append_logs(logfile, options, stdout, stderr)


def do_portscan(host, options, logfile, stop_event):
    """
    Perform a portscan.

    Args:
        host:       Target host.
        options:    Dictionary object containing options.
        logfile:    Filename where logfile will be written to.
        stop_event: Event handler for stop event

    Returns:
        A list of open ports.
    """
    if not options['nmap']:
        return ALLPORTS
    open_ports = []
    arguments = '--open'
    if is_admin():
        arguments += ' -sS'
        if options['udp']:
            arguments += ' -sU'
    else:
        arguments += ' -sT'
    if options['port']:
        arguments += ' -p' + options['port']
    if options['no_portscan']:
        arguments = '-sn -Pn'
    arguments += ' -sV --script=' + SCRIPTS
    if options['whois']:
        arguments += ',asn-query,fcrdns,whois-ip'
        if re.match('.*[a-z].*', host):
            arguments += ',whois-domain'
    if options['allports']:
        arguments += ' -p1-65535'
    if options['dry_run']:
        return ALLPORTS
    logging.info('%s Starting nmap', host)
    try:
        temp_file = 'nmap-{0}-{1}'.format(host, next(tempfile._get_candidate_names()))  # pylint: disable=protected-access
        arguments = '{0} -oN {1}'.format(arguments, temp_file)
        scanner = nmap.PortScanner()
        logging.debug('nmap %s %s', arguments, host)
        scanner.scan(hosts=host, arguments=arguments)
        for ip_address in [x for x in scanner.all_hosts() if scanner[x] and
                           scanner[x].state() == 'up']:
            open_ports = [port for port in scanner[ip_address].all_tcp() if
                          scanner[ip_address]['tcp'][port]['state'] == 'open']
        if options['no_portscan'] or len(open_ports):
            append_file(logfile, options, temp_file)
            if len(open_ports):
                logging.info('%s Found open ports %s', host, open_ports)
    except (AssertionError, nmap.PortScannerError) as exception:
        if stop_event.isSet():
            logging.debug('nmap interrupted')
        else:
            logging.error('Issue with nmap (%s)', exception)
        open_ports = [UNKNOWN]
    finally:
        if os.path.isfile(temp_file):
            os.remove(temp_file)
    return open_ports


def do_testssl(host, port, options, logfile):
    """
    Check SSL/TLS configuration and vulnerabilities.
    """
    command = ['testssl.sh', '--quiet', '--warnings', 'off', '--color', '0',
               '-p', '-f', '-U', '-S']
    if options['timeout']:
        command = ['timeout', str(options['maxtime'])] + command
    if port == 25:
        command += ['--starttls', 'smtp']
    logging.debug('%s Starting testssl.sh on port %s', host, port)
    _result, stdout, stderr = execute_command(command +  # pylint: disable=unused-variable
                                              ['{0}:{1}'.format(host, port)],
                                              options)
    append_logs(logfile, options, stdout, stderr)


def do_wpscan(url, options, logfile):
    """
    Run WPscan/
    """
    if options['wpscan']:
        logging.debug('Starting WPscan on ' + url)
        command = ['wpscan', '--batch', '--no-color', '--url', url]
        _result, stdout, stderr = execute_command(command, options)  # pylint: disable=unused-variable
        append_logs(logfile, options, stdout, stderr)


def prepare_queue(options):
    """
    Prepare a queue file which holds all hosts to scan.
    """
    expanded = False
    if not options['inputfile']:
        expanded = next(tempfile._get_candidate_names())  # pylint: disable=protected-access
        with open(expanded, 'a') as inputfile:
            inputfile.write(options['target'])
        options['inputfile'] = expanded
    with open(options['inputfile'], 'r') as inputfile:
        hosts = inputfile.read().splitlines()
        targets = []
        for host in hosts:
            if re.match(r'.*\.[0-9]+[-/][0-9]+', host) and not options['dry_run']:
                if not options['nmap']:
                    logging.error('nmap is necessary for IP ranges')
                arguments = '-nsL'
                scanner = nmap.PortScanner()
                scanner.scan(hosts='{0}'.format(host), arguments=arguments)
                targets += sorted(scanner.all_hosts(),
                                  key=lambda x: tuple(map(int, x.split('.'))))
            else:
                targets.append(host)
        with open(options['queuefile'], 'a') as queuefile:
            for target in targets:
                queuefile.write(target + '\n')
    if expanded:
        os.remove(expanded)


def remove_from_queue(host, options):
    """
    Remove a host from the queue file.
    """
    with open(options['queuefile'], 'r+') as queuefile:
        hosts = queuefile.read().splitlines()
        queuefile.seek(0)
        for i in hosts:
            if i != host:
                queuefile.write(i + '\n')
        queuefile.truncate()
    if not os.stat(options['queuefile']).st_size:
        os.remove(options['queuefile'])


def port_open(port, open_ports):
    """
    Check whether a port has been flagged as open.
    Returns True if the port was open, or hasn't been scanned.

    Arguments:
    - `port`: the port to look up
    - `open_ports`: a list of open ports, or -1 if it hasn't been scanned.
    """
    return (UNKNOWN in open_ports) or (port in open_ports)


def use_tool(tool, host, port, options, logfile):
    """
    Wrapper to see if tool is available, and to start correct tool.
    """
    if not options[tool]:
        return
    logging.debug('starting %s scan on %s:%s', tool, host, port)
    if tool == 'nikto':
        do_nikto(host, port, options, logfile)
    if tool == 'curl':
        do_curl(host, port, options, logfile)
    if tool == 'testssl.sh':
        do_testssl(host, port, options, logfile)


def process_host(options, host_queue, output_queue, stop_event):
    """
    Worker thread: Process each host atomic, add output files to output_queue
    """
    while host_queue.qsize() and not stop_event.wait(.01):
        try:
            host = host_queue.get()
            host_logfile = host + '-' + next(tempfile._get_candidate_names())  # pylint: disable=protected-access
            logging.debug('%s Processing (%s in queue)', host, host_queue.qsize())
            open_ports = do_portscan(host, options, host_logfile, stop_event)
            if len(open_ports):
                if UNKNOWN in open_ports:
                    logging.info('%s Scan interrupted ?', host)
                else:
                    append_logs(host_logfile, options, '{0} Open ports: {1}'.
                                format(host, open_ports))
                    for port in open_ports:
                        if stop_event.isSet():
                            logging.info('%s Scan interrupted ?', host)
                            break
                        if port in [80, 443, 8080]:
                            analyze_url(host, port, options, host_logfile)
                            for tool in ['curl', 'nikto']:
                                use_tool(tool, host, port, options, host_logfile)
                        if port in [25, 443, 465, 993, 995]:
                            for tool in ['testssl.sh']:
                                use_tool(tool, host, port, options, host_logfile)
                            download_cert(host, port, options, host_logfile)
            else:
                logging.info('%s Nothing to report', host)
            if os.path.isfile(host_logfile) and os.stat(host_logfile).st_size:
                with open(host_logfile, 'r') as read_file:
                    output_queue.put(read_file.read())
                os.remove(host_logfile)
            if UNKNOWN not in open_ports:
                remove_from_queue(host, options)
            host_queue.task_done()
        except Queue.Empty:
            break
    logging.debug('Exiting process_host thread')


def process_output(output_queue, stop_event):
    """
    Process logfiles synchronously.
    """
    while not stop_event.wait(1) or not output_queue.empty():
        try:
            item = output_queue.get(block=False)
            logging.info(item.encode('ascii', 'ignore'))
            output_queue.task_done()
        except Queue.Empty:
            pass
    logging.debug('Finished process_output')


def loop_hosts(options, queue):
    """
    Main loop, iterate all hosts in queue and perform requested actions.
    """
    stop_event = threading.Event()
    work_queue = Queue.Queue()
    output_queue = Queue.Queue()

    def stop_gracefully(signum, frame):  # pylint: disable=unused-argument
        """
        Handle interrupt (gracefully).
        """
        logging.error('Caught Ctrl-C - exiting gracefully (please be patient)')
        stop_event.set()

    signal.signal(signal.SIGINT, stop_gracefully)
    for host in queue:
        work_queue.put(host)
    threads = [threading.Thread(target=process_host, args=(options, work_queue,
                                                           output_queue,
                                                           stop_event))
               for _ in range(min(options['threads'] - 1, work_queue.qsize()))]
    threads.append(threading.Thread(target=process_output, args=(output_queue,
                                                                 stop_event)))
    logging.debug('Starting %s threads', len(threads))
    for thread in threads:
        thread.start()
    while work_queue.qsize() and not stop_event.wait(1):
        try:
            time.sleep(1)
        except IOError:
            pass
    if not stop_event.isSet():
        work_queue.join()  # block until the queue is empty
        stop_event.set()  # signal that the work_queue is empty
    logging.debug('Waiting for threads to finish')
    while threads:
        threads.pop().join()
    if output_queue.qsize():
        process_output(output_queue, stop_event)
    output_queue.join()  # always make sure that the output is properly processed


def read_queue(filename):
    """
    Return a list of targets.
    """
    queue = []
    try:
        with open(filename, 'r') as queuefile:
            queue = queuefile.read().splitlines()
    except IOError:
        logging.error('Could not read %s', filename)
    return queue


def parse_arguments(banner):
    """
    Parse command line arguments.
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(banner + '''\
 - scans one or more hosts for security misconfigurations

Please note that this is NOT a stealthy scan tool: By default, a TCP and UDP
portscan will be launched, using some of nmap's interrogation scripts.

Copyright (C) 2015-2016  Peter Mosmans [Go Forward]
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.'''))
    parser.add_argument('target', nargs='?', type=str,
                        help="""[TARGET] can be a single (IP) address, an IP
                        range, eg. 127.0.0.1-255, or multiple comma-separated
                        addressess""")
    parser.add_argument('--dry-run', action='store_true',
                        help='only show commands, don\'t actually do anything')
    parser.add_argument('-i', '--inputfile', action='store', type=str,
                        help='a file containing targets, one per line')
    parser.add_argument('-o', '--output-file', action='store', type=str,
                        default='analyze_hosts.output',
                        help="""output file containing all scanresults
                        (default analyze_hosts.output""")
    parser.add_argument('--nikto', action='store_true',
                        help='run a nikto scan')
    parser.add_argument('-n', '--no-portscan', action='store_true',
                        help='do NOT run a nmap portscan')
    parser.add_argument('-p', '--port', action='store',
                        help='specific port(s) to scan')
    parser.add_argument('--compact', action='store_true',
                        help='log as little as possible')
    parser.add_argument('--queuefile', action='store',
                        default='analyze_hosts.queue', help='the queuefile')
    # parser.add_argument('--quiet', action='store_true',
    #                     help='do not show logfiles on the console')
    parser.add_argument('--resume', action='store_true',
                        help='resume working on the queue')
    parser.add_argument('--ssl', action='store_true',
                        help='run a ssl scan')
    parser.add_argument('--sslcert', action='store_true',
                        help='download SSL certificate')
    parser.add_argument('--threads', action='store', type=int, default=5,
                        help='maximum number of threads')
    parser.add_argument('--udp', action='store_true',
                        help='check for open UDP ports as well')
    parser.add_argument('--framework', action='store_true',
                        help='analyze the website and run webscans')
    parser.add_argument('--allports', action='store_true',
                        help='run a full-blown nmap scan on all ports')
    parser.add_argument('-t', '--trace', action='store_true',
                        help='check webserver for HTTP TRACE method')
    parser.add_argument('-w', '--whois', action='store_true',
                        help='perform a whois lookup')
    parser.add_argument('--header', action='store', default='analyze_hosts',
                        help='custom header to use for scantools')
    parser.add_argument('--maxtime', action='store', default='1200', type=int,
                        help='timeout for scans in seconds (default 1200)')
    parser.add_argument('--timeout', action='store', default='10', type=int,
                        help='timeout for requests in seconds (default 10)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Be more verbose')
    args = parser.parse_args()
    if not (args.inputfile or args.target or args.resume):
        parser.error('Specify either a target or input file')
    options = vars(parser.parse_args())
    options['testssl.sh'] = args.ssl
    options['curl'] = args.trace
    options['wpscan'] = args.framework
    options['droopescan'] = args.framework
    return options


def setup_logging(options):
    """
    Set up loghandlers according to options.
    """
    # DEBUG = verbose status messages
    # INFO = status messages and logfiles
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logfile = logging.FileHandler(options['output_file'])
    logfile.setFormatter(logging.Formatter('%(asctime)s %(message)s',
                                           datefmt='%m-%d-%Y %H:%M'))
    logfile.setLevel(logging.INFO)
    logger.addHandler(logfile)
    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(logging.Formatter('%(asctime)s %(message)s',
                                           datefmt='%H:%M:%S'))
    if options['verbose']:
        console.setLevel(logging.DEBUG)
    else:
        console.setLevel(logging.INFO)
    logger.addHandler(console)


def main():
    """
    Main program loop.
    """
    banner = 'analyze_hosts.py version {0}'.format(VERSION)
    options = parse_arguments(banner)
    setup_logging(options)
    logging.info(banner + ' starting')
    preflight_checks(options)
    if not options['resume']:
        prepare_queue(options)
    queue = read_queue(options['queuefile'])
    loop_hosts(options, queue)
    if not options['dry_run']:
        logging.debug('Output saved to %s', options['output_file'])
    sys.exit(0)


if __name__ == "__main__":
    main()
