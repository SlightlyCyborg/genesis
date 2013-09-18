import os
import getopt
import iptc

from genesis.ui import UI
from genesis.utils import shell, cidr_to_netmask
from genesis.api import *
from genesis import apis
from genesis.com import *

from genesis.plugins.network.servers import ServerManager


class RuleManager(Plugin):
    rules = []

    def set(self, server, allow):
        self.app.gconfig.set('security', 'fw-%s-%s'
            %(server.plugin_id, server.server_id), allow)
        self.app.gconfig.save()

    def get(self, server):
        for x in ServerManager().get_all():
            if x.server == server:
                return int(self.app.gconfig.get('security', 'fw-%s-%s'
                    %(x.plugin_id, x.server_id)))
        return False

    def get_by_id(self, id):
        for x in ServerManager().get_all():
            if x.server.server_id == id:
                return (x, int(self.app.gconfig.get('security', 'fw-%s-%s'
                    %(x.plugin_id, x.server_id))))
        return False

    def get_all(self):
        rules = []
        for x in ServerManager().get_all():
            rules.append((x, int(self.app.gconfig.get('security', 'fw-%s-%s'
                %(x.plugin_id, x.server_id)))))
        return rules

    def scan_servers(self):
        # Scan active servers and create entries for them when necessary
        for x in ServerManager().get_all():
            if not self.app.gconfig.has_option('security', 'fw-%s-%s'
                %(x.plugin_id, x.server_id)):
                self.set(x, 2)

    def clear_cache(self):
        # Compares active firewall preferences stored in config
        # to active servers, removes obsolete entries
        s = ServerManager().get_all()
        r = re.compile('fw-((?:[a-z][a-z]+))-((?:[a-z][a-z]+))',
            re.IGNORECASE)
        for o in self.app.gconfig.options('security'):
            m = r.match(o)
            if m:
                pid, sid = m.group(1), m.group(2)
                for x in s:
                    present = False
                    if x.plugin_id == pid and x.server_id == sid:
                        present = True
                    if present == False:
                        self.remove(o)

    def remove(self, server):
        # Remove an entry from firewall config
        self.app.gconfig.remove_option('security', 'fw-%s-%s'
            %(server.plugin_id, server.server_id))
        self.app.gconfig.save()

    def remove_by_plugin(self, id):
        # Remove all entries for a particular plugin
        r = re.compile('fw-((?:[a-z][a-z]+))-((?:[a-z][a-z]+))',
            re.IGNORECASE)
        for o in self.app.gconfig.options('security'):
            m = r.match(o)
            if m and m.group(1) == id:
                self.app.gconfig.remove_option('security', o)
        self.app.gconfig.save()


class FWMonitor(Plugin):
    def scan(self):
        # Update our local configs from what is in our iptables chain.
        # This should probably never be used, but it looks pretty.
        rm = RuleManager()
        tb = iptc.Table(iptc.Table.FILTER)
        c = iptc.Chain(tb, "genesis-apps")
        if not tb.is_chain(c):
            tb.create_chain(c)
            return
        for r in c.rules:
            m = r.matches[0]
            for s in ServerManager().get_by_port(m.dport):
                srv = rm.get(s)
                if '0.0.0.0/255.255.255.255' in r.src:
                    rm.set(s, 2)
                else:
                    rm.set(s, 1)

    def regen(self, range):
        # Regenerate our chain.
        self.flush()
        for x in RuleManager().get_all():
            for p in x[0].server.port:
                if x[1] == 2:
                    self.add(p[0], p[1], '0.0.0.0')
                elif x[1] == 1:
                    for r in range:
                        self.add(p[0], p[1], r)
                else:
                    self.remove(p[0], p[1])

    def add(self, protocol, port, range=''):
        # Add rule for this port
        # If range is not provided, assume '0.0.0.0'
        tb = iptc.Table(iptc.Table.FILTER)
        c = iptc.Chain(tb, "genesis-apps")
        if not tb.is_chain(c):
            tb.create_chain(c)
        r = iptc.Rule()
        r.protocol = protocol
        if range == '':
            r.src = '0.0.0.0/255.255.255.255'
        else:
            ip, cidr = range.split('/')
            mask = cidr_to_netmask(int(cidr))
            r.src = ip + '/' + mask
        m = iptc.Match(r, protocol)
        m.dport = port
        r.add_match(m)
        t = iptc.Target(r, 'ACCEPT')
        r.target = t
        c.insert_rule(r)

    def remove(self, protocol, port, range=''):
        # Remove rule(s) in our chain matching this port
        # If range is not provided, delete all rules for this port
        tb = iptc.Table(iptc.Table.FILTER)
        c = iptc.Chain(tb, "genesis-apps")
        if not tb.is_chain(c):
            return
        for r in c.rules:
            if range != '':
                if r.matches[0].dport == port and range in r.dst:
                    c.delete_rule(r)
            else:
                if r.matches[0].dport == port:
                    c.delete_rule(r)

    def find(self, protocol, port, range=''):
        # Returns true if rule is found for this port
        # If range IS provided, return true only if range is the same
        tb = iptc.Table(iptc.Table.FILTER)
        c = iptc.Chain(tb, "genesis-apps")
        if not tb.is_chain(c):
            return False
        for r in c.rules:
            if range != '':
                if r.matches[0].dport == port and range in r.dst:
                    return True
            elif range == '' and r.matches[0].dport == port:
                return True
        return False

    def flush(self):
        # Flush out our chain
        tb = iptc.Table(iptc.Table.FILTER)
        c = iptc.Chain(tb, "genesis-apps")
        if not tb.is_chain(c):
            return
        c.flush()

    def save(self):
        # Save rules to file loaded on boot
        pass


# Keeping this section for advanced use, for now.
# TODO: Migrate this where possible to calls for python-iptables

class Rule:
    states = ['NEW', 'ESTABLISHED', 'RELATED', 'INVALID']
    flags = ['SYN', 'ACK', 'FIN', 'RST', 'URG', 'PSH', 'ALL', 'NONE']

    def __init__(self, line='-A INPUT -j ACCEPT'):
        self.reset()
        self.raw = line
        opts = line.split()
        self.desc = ' '.join(opts[2:-2])

        while len(opts) > 0:
            inv = False
            if opts[0] == '!':
                inv = True
                opts = opts[1:]
            s = [opts[0]]
            prefix = ''
            while s[0].startswith('-'):
                prefix += s[0][0]
                s[0] = s[0][1:]
            opts = opts[1:]
            while len(opts) > 0 and not opts[0].startswith('-'):
                if opts[0] == '!':
                    break
                else:
                    s.append(opts[0])
                    opts = opts[1:]

            # S is one option
            if s[0] == 'f':
                self.fragment = (inv, True)
                continue
            if s[0] == 'A':
                self.chain = s[1]
                continue
            if s[0] == 'j':
                self.action = s[1]
                continue
            if s[0] == 'm':
                self.modules.append(s[1])
                continue
            if s[0] == 'tcp-flags':
                self.tcp_flags = (inv, s[1].split(','), s[2].split(','))
                continue
            if s[0] == 'state':
                self.state = (inv, s[1].split(','))
                continue

            self.tryset('protocol', inv, s, 'p', 'protocol') or \
            self.tryset('source', inv, s, 's', 'src') or \
            self.tryset('destination', inv, s, 'd', 'dst') or \
            self.tryset('mac_source', inv, s, 'mac-source') or \
            self.tryset('in_interface', inv, s, 'i', 'in-interface') or \
            self.tryset('out_interface', inv, s, 'i', 'in-interface') or \
            self.tryset('sport', inv, s, 'sport', 'source-port') or \
            self.tryset('dport', inv, s, 'dport', 'destination-port') or \
            self.tryset('sport', inv, s, 'sports', 'source-ports') or \
            self.tryset('dport', inv, s, 'dports', 'destination-ports') or \
            self.add_option(inv, prefix, s)


    def get_ui_text(self, param, help=''):
        v = getattr(self, param)
        return UI.HContainer(
                    UI.SelectInput(
                        UI.SelectOption(text='Ign.', value='ign', selected=v[1] is None),
                        UI.SelectOption(text='Is', value='nrm', selected=not v[0] and v[1] is not None),
                        UI.SelectOption(text='Isn\'t', value='inv', selected=v[0] and v[1] is not None),
                        design='mini',
                        name='%s-mode'%param
                    ),
                    UI.TextInput(name=param, value=v[1] or '', help=help),
                    spacing=5
               )

    def get_ui_bool(self, param):
        v = getattr(self, param)
        return UI.HContainer(
                    UI.SelectInput(
                        UI.SelectOption(text='Ign.', value='ign', selected=v[1] is None),
                        UI.SelectOption(text='Yes', value='nrm', selected=v[1]==True),
                        UI.SelectOption(text='No', value='inv', selected=v[1]==False),
                        design='mini',
                        name='%s-mode'%param
                    ),
                    spacing=5
               )

    def get_ui_select(self, param, opts):
        # opts == [['Desc', 'value'], ['Desc #2', 'value2']]
        v = getattr(self, param)

        return UI.HContainer(
                    UI.SelectInput(
                        UI.SelectOption(text='Ign.', value='ign', selected=v[1] is None),
                        UI.SelectOption(text='Is', value='nrm', selected=not v[0] and v[1] is not None),
                        UI.SelectOption(text='Isn\'t', value='inv', selected=v[0] and v[1] is not None),
                        design='mini',
                        name='%s-mode'%param
                    ),
                    UI.SelectTextInput(
                        *[UI.SelectOption(text=x[0], value=x[1], selected=v[1]==x[1])
                            for x in opts],
                        name=param,
                        value=v[1] or '',
                        design='mini'
                    ),
                    spacing=5
               )

    def get_ui_flags(self):
        v = self.tcp_flags

        return UI.HContainer(
                    UI.SelectInput(
                        UI.SelectOption(text='Ign.', value='ign', selected=v[1] is None),
                        UI.SelectOption(text='Are', value='nrm', selected=not v[0] and v[1] is not None),
                        UI.SelectOption(text='Are not', value='inv', selected=v[0] and v[1] is not None),
                        design='mini',
                        name='tcpflags-mode'
                    ),
                        UI.LT(
                            UI.LTR(
                                UI.Label(text='Check:'),
                                *[UI.Checkbox(text=x, name='tcpflags-vals[]', value=x, checked=x in v[2] if v[2] else False)
                                    for x in self.flags]
                            ),
                            UI.LTR(
                                UI.Label(text='Mask:'),
                                *[UI.Checkbox(text=x, name='tcpflags-mask[]', value=x, checked=x in v[1] if v[1] else False)
                                    for x in self.flags]
                            )
                        ),
               )

    def get_ui_states(self):
        v = self.state
        return UI.HContainer(
                    UI.SelectInput(
                        UI.SelectOption(text='Ign.', value='ign', selected=v[1] is None),
                        UI.SelectOption(text='Is', value='nrm', selected=not v[0] and v[1] is not None),
                        UI.SelectOption(text='Isn\'t', value='inv', selected=v[0] and v[1] is not None),
                        design='mini',
                        name='state-mode',
                    ),
                    UI.HContainer(
                        *[UI.Checkbox(text=x, name='state[]', value=x, checked=v[1] and x in v[1])
                            for x in self.states]
                    )
               )

    def tryset(self, param, inv, args, *names):
        if args[0] in names:
            setattr(self, param, (inv, ' '.join(args[1:])))
        return args[0] in names

    def add_option(self, inv, prefix, s):
        self.miscopts.append(('! ' if inv else '') + prefix + ' '.join(s))

    def reset(self):
        self.action = 'ACCEPT'
        self.chain = 'INPUT'
        self.miscopts = []
        self.modules = []
        self.tcp_flags = (False, None, None)

    def __getattr__(self, attr):
        return (False, None)

    def dump(self):
        return self.raw

    def apply_vars(self, vars):
        line = '-A ' + self.chain

        self.modules = vars.getvalue('modules', '').split()
        for m in self.modules:
            line += ' -m ' + m

        line += self._format_option('-p', 'protocol', vars)
        line += self._format_option('-s', 'source', vars)
        line += self._format_option('-d', 'destination', vars)
        line += self._format_option('--mac-source', 'mac_source', vars, module='mac')
        line += self._format_option('-i', 'in_interface', vars)
        line += self._format_option('-o', 'out_interface', vars)

        line += self._format_option('--sports', 'sport', vars, module='multiport')
        line += self._format_option('--dports', 'dport', vars, module='multiport')

        if vars.getvalue('fragmented-mode', '') == 'nrm':
            line += ' -f'
        if vars.getvalue('fragmented-mode', '') == 'inv':
            line += ' ! -f'

        if vars.getvalue('tcpflags-mode', '') != 'ign':
            if vars.getvalue('tcpflags-mode', '') == 'inv':
                line += ' !'

            mask = []
            for i in range(0, len(self.flags)):
                if vars.getvalue('tcpflags-mask[]')[i] == '1':
                    mask.append(self.flags[i])
            vals = []
            for i in range(0, len(self.flags)):
                if vars.getvalue('tcpflags-vals[]')[i] == '1':
                    vals.append(self.flags[i])

            if mask == []:
                mask = ['NONE']
            if vals == []:
                vals = ['NONE']

            line += ' --tcp-flags ' + ','.join(mask) + ' '  + ','.join(vals)

        if vars.getvalue('state-mode', '') != 'ign':
            if not 'state' in self.modules:
                line += ' -m state'
            if vars.getvalue('state-mode', '') == 'inv':
                line += ' !'
            st = []
            for i in range(0, len(self.states)):
                if vars.getvalue('state[]')[i] == '1':
                    st.append(self.states[i])
            if st == []:
                st = ['NONE']
            line += ' --state ' + ','.join(st)

        line += ' ' + ' '.join(self.miscopts)

        self.action = vars.getvalue('caction', 'ACCEPT')
        if self.action == 'RUN':
            self.action = vars.getvalue('runchain', 'ACCEPT')

        line += ' -j ' + self.action

        self.__init__(line)


    def _format_option(self, name, key, vars, flt=lambda x: x, module=None):
        if vars.getvalue(key+'-mode') == 'ign':
            return ''
        s = ''
        if module is not None:
            if not module in self.modules:
                self.modules.append(module)
                s = ' -m '+ module
        if vars.getvalue(key+'-mode') == 'nrm':
            s += ' ' + name + ' ' + flt(vars.getvalue(key, ''))
        if vars.getvalue(key+'-mode') == 'inv':
            s += ' ! ' + name + ' ' + flt(vars.getvalue(key, ''))
        return s


class Chain:
    rules = None

    def __init__(self, name, default):
        self.rules = []
        self.name = name
        self.comment = None
        self.default = default

    def dump(self):
        s = ''
        for r in self.rules:
            s += '%s\n' % r.dump()
        return s


class Table:
    chains = None

    def __init__(self, name):
        self.chains = {}
        self.name = name

    def load(self, data):
        while len(data)>0:
            s = data[0]
            if s.startswith('*'):
                return
            elif s.startswith(':'):
                n,d = s.split()[0:2]
                n = n[1:]
                self.chains[n] = Chain(n, d)
            elif s.startswith('-'):
                r = Rule(s)
                self.chains[r.chain].rules.append(r)
            data = data[1:]

    def dump(self):
        s = '*%s\n' % self.name
        for r in self.chains:
            r = self.chains[r]
            s += ':%s %s [0:0]\n' % (r.name, r.default)
        for r in self.chains:
            r = self.chains[r]
            s += '%s' % r.dump()
        s += 'COMMIT\n'
        return s


class Config(Plugin):
    implements(IConfigurable)
    name = 'iptables'
    iconfile = 'gen-fire'
    id = 'iptables'
    tables = {}
    apply_shell = 'cat /etc/iptables.up.rules | iptables-restore'

    def __init__(self):
        if self.app.config.has_option('iptables', 'rules_file'):
            self.rules_file = self.app.config.get('iptables', 'rules_file')
        else:
            cfg = self.app.get_backend(IConfig)
            if hasattr(cfg, 'rules_file'):
                self.rules_file = cfg.rules_file
            elif os.path.exists('/etc/iptables'):
                self.rules_file = '/etc/iptables/rules'
            else:
                self.rules_file = '/etc/iptables.up.rules' # webmin import
        self.apply_shell = 'cat %s | iptables-restore' % self.rules_file

    def list_files(self):
        return [self.rules_file]

    def load_runtime(self):
        shell('iptables -L -t filter')
        shell('iptables -L -t mangle')
        shell('iptables -L -t nat')
        shell('iptables-save > %s' % self.rules_file)
        self.load()

    def apply_now(self):
        return shell(self.apply_shell)

    def has_autostart(self):
        b = self.app.get_backend(IConfig)
        return b.has_autostart()

    def set_autostart(self, active):
        b = self.app.get_backend(IConfig)
        b.set_autostart(active)

    def load(self, file=None):
        file = file or self.rules_file
        self.tables = {}
        try:
            data = ConfManager.get().load('iptables', file).split('\n')
            while len(data)>0:
                s = data[0]
                data = data[1:]
                if s != '':
                    if s[0] == '*':
                        self.tables[s[1:]] = Table(s[1:])
                        self.tables[s[1:]].load(data)
        except:
            pass

    def get_devices(self):
        d = []
        for l in open('/proc/net/dev').read().splitlines():
            if ':' in l:
                dev = l.split(':')[0].strip()
                d.append((dev,dev))
        return d

    def dump(self):
        s = ''
        for r in self.tables:
            s += '%s\n' % self.tables[r].dump()
        return s

    def save(self, file=None):
        file = file or self.rules_file
        ConfManager.get().save('iptables', file, self.dump())
        ConfManager.get().commit('iptables')

    def table_index(self, name):
        i = 0
        for t in self.tables:
            if self.tables[t].name == name:
                return i
            i += 1


class IConfig(Interface):
    def has_autostart(self):
        pass

    def set_autostart(self, active):
        pass


class DebianConfig(Plugin):
    implements(IConfig)
    platform = ['debian', 'ubuntu']
    path = '/etc/network/if-up.d/iptables'

    @property
    def apply_shell(self):
        return '#!/bin/sh\ncat \'%s\' | iptables-restore' % Config(self.app).rules_file

    def has_autostart(self):
        return os.path.exists(self.path)

    def set_autostart(self, active):
        if active:
            open(self.path, 'w').write(self.apply_shell)
            shell('chmod 755 ' + self.path)
        else:
            try:
                os.unlink(self.path)
            except:
                pass


class ArchConfig(Plugin):
    implements(IConfig)
    platform = ['arch', 'arkos']
    path = '/etc/systemd/system/multi-user.target.wants/iptables.service'

    @property
    def apply_shell(self):
        return '#!/bin/sh\ncat \'%s\' | iptables-restore' % Config(self.app).rules_file

    def has_autostart(self):
        return os.path.exists(self.path)

    def set_autostart(self, active):
        if active:
            os.symlink('/usr/lib/systemd/system/iptables.service', self.path)
        else:
            try:
                os.unlink(self.path)
            except:
                pass


class GentooConfig(Plugin):
    implements(IConfig)
    platform = ['gentoo']
    rules_file = '/var/lib/iptables/rules-save'

    @property
    def apply_shell(self):
        return '#!/bin/sh\ncat \'%s\' | iptables-restore' % Config(self.app).rules_file

    def has_autostart(self):
        return True

    def set_autostart(self, active):
        pass


class CentosConfig(Plugin):
    implements(IConfig)
    platform = ['centos']
    rules_file = '/etc/sysconfig/iptables'

    @property
    def apply_shell(self):
        return '#!/bin/sh\ncat \'%s\' | iptables-restore' % Config(self.app).rules_file

    def has_autostart(self):
        return True

    def set_autostart(self, active):
        pass
