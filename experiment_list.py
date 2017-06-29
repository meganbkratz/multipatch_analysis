# *-* coding: utf-8 *-*
from __future__ import print_function, division
import numpy as np
import os
import pickle
import scipy.optimize
import scipy.stats
import sys
import traceback
import warnings

import pyqtgraph as pg

from graphics import MatrixItem, distance_plot
from experiment import Experiment
from constants import INHIBITORY_CRE_TYPES, EXCITATORY_CRE_TYPES


class Entry(object):
    def __init__(self, line, parent, file, lineno):
        self.indentation = indentation(line)
        self.lines = []
        self.add_line(line)
        self.parent = parent
        self.file = None if file is None else os.path.abspath(file)
        self.lineno = lineno
        self.children = []
        if parent is not None:
            parent.add_child(self)

    def add_line(self, line):
        if indentation(line) != self.indentation:
            raise IndentationError(line)
        self.lines.append(line[self.indentation:].rstrip())

    def add_child(self, child):
        self.children.append(child)

    def print_tree(self):
        print("\n".join([('    '*self.indentation)+l for l in self.lines]))
        for ch in self.children:
            ch.print_tree()


def indentation(line):
    return len(line) - len(line.lstrip('- '))


class ExperimentList(object):

    def __init__(self, expts=None, cache=None):
        self._cache_version = 3
        self._cache = cache
        self._expts = []
        self._expts_by_id = {}
        self.start_skip = []
        self.stop_skip = []

        if expts is not None:
            for expt in expts:
                self._add_experiment(expt)
        if cache is not None:
            try:
                self.load(cache)
            except Exception:
                sys.excepthook(*sys.exc_info())
                print('Error reading cache file "%s". (exception printed above)' % cache)

    def load(self, filename):
        if filename.endswith('.pkl'):
            self._load_pickle(filename)
        else:
            self._load_text(filename)

    def _load_pickle(self, filename):
        el = pickle.load(open(filename))
        ver = getattr(el, '_cache_version', None)
        if ver != self._cache_version:
            print("Ignoring cache file %s due to incompatible version (%s != %s)" % (filename, ver, self._cache_version))
            return
        for expt in el._expts:
            self._add_experiment(expt)
        self.sort()

    def _load_text(self, filename):
        root = Entry('', None, None, None)
        root.indentation = -1
        current = root

        lines = open(filename, 'r').readlines()

        for i,line in enumerate(lines):
            if line.lstrip().startswith('#'):
                continue

            if line.strip() == '':
                continue
            ind = indentation(line)
            if ind > current.indentation:
                ch = Entry(line, parent=current, file=filename, lineno=i)
                current = ch
            else:
                while current.indentation > ind:
                    current = current.parent
                ch = Entry(line, parent=current.parent, file=filename, lineno=i)
                current = ch
                continue

        # Parse experiment data
        errs = []
        cached = 0
        for entry in root.children:
            try:
                expt_id = Experiment.id_from_entry(entry)
                if expt_id in self._expts_by_id:
                    # Already have this experiment cached
                    cached += 1
                    continue
                expt = Experiment(entry)
            except Exception as exc:
                errs.append((entry, sys.exc_info()))
                continue

            self._add_experiment(expt)

        if len(errs) > 0:
            print("Errors loading %d experiments:" % len(errs))
            for entry, exc in errs:
                print("=======================")
                print("\n".join(entry.lines))
                traceback.print_exception(*exc)
                print("")
        if cached > 0:
            print("Skipped loading %d experiments (already cached)" % cached)

        self.sort()

    def _add_experiment(self, expt):
        self._expts.append(expt)
        self._expts_by_id[expt.expt_id] = expt

    def write_cache(self):
        if self._cache is None:
            raise Exception("ExperimentList has no cache file; cannot write cache.")
        pickle.dump(self, open(self._cache, 'w'))

    def select(self, start=None, stop=None, region=None, source_files=None, cre_type=None, calcium=None, age=None, temp=None):
        expts = []
        for ex in self._expts:
            # filter experiments by experimental date and conditions
            if calcium is not None and calcium != 'compare':
                if 'solution' in ex.expt_info:
                    if '2mM' in ex.expt_info['solution']:
                        ex_calcium = 'high'
                    elif '1.3mM' in ex.expt_info['solution']:
                        ex_calcium = 'low'
                else:
                    print("External calcium concentration not set for experiment %s" % str(ex.expt_id))
                    continue
            else:
                ex_calcium = "compare"
            if start is not None and ex.date < start:
                continue
            elif stop is not None and ex.date > stop:
                continue
            elif region is not None and ex.region != region:
                continue
            elif source_files is not None and ex.expt_id[0] not in source_files:
                continue
            elif cre_type is not None and len(set(cre_type) & set(ex.cre_types)) == 0:
                continue
            elif calcium is not None and calcium.lower() != ex_calcium:
                continue
            elif age is not None and ((ex.age < age[0]) or (ex.age > age[-1])):
                continue
            elif temp is not None and ex.expt_info['temperature'][:2] != temp:
                continue
            else:
                expts.append(ex)

        el = ExperimentList(expts)
        return el

    def __getitem__(self, item):

        return self._expts[item]

    def __len__(self):
        return len(self._expts)

    def __iter__(self):
        return self._expts.__iter__()

    def append(self, expt):
        self._expts.append(expt)

    def sort(self, key=lambda expt: expt.expt_id[1], **kwds):
        self._expts.sort(key=key, **kwds)

    def check(self):
        # sanity check: all experiments should have cre and fl labels
        for expt in self:
            # make sure we have at least one non-biocytin label and one cre label
            if len(expt.cre_types) < 1:
                print("Warning: Experiment %s has no cre-type labels" % str(expt.expt_id))
            if len(expt.labels) < 1 or expt.labels == ['biocytin']:
                print("Warning: Experiment %s has no fluorescent labels" % str(expt.expt_id))
            if expt.region is None:
                print("Warning: Experiment %s has no region" % str(expt.expt_id))

    def distance_plot(self, pre_type, post_type, calcium, age, plots=None, color=(100, 100, 255)):
        # get all connected and unconnected distances for pre->post
        probed = []
        connected = []
        if calcium is not None:
            el = self.select(calcium=calcium)
            legend = 'Ca = %s' % calcium
        elif age is not None:
            el = self.select(age=age)
            legend = 'age = p%d - %d' % (age[0], age[1])
        else:
            el = self
            legend = ''
        for expt in el:
            for i,j in expt.connections_probed:
                ci, cj = expt.cells[i], expt.cells[j]
                if ci.cre_type != pre_type or cj.cre_type != post_type:
                    continue
                dist = ci.distance(cj)
                probed.append(dist)
                connected.append((i, j) in expt.connections)

        return distance_plot(connected, distance=probed, plots=plots, color=color, name=("%s->%s "%(pre_type, post_type)) + legend)

    def matrix(self, rows, cols, size=50):
        w = pg.GraphicsLayoutWidget()
        v = w.addViewBox()
        v.setAspectLocked()
        v.invertY()

        colormap = pg.ColorMap(
            [0, 0.01, 0.03, 0.1, 0.3, 1.0],
            [(0,0,100), (80,0,80), (140,0,0), (255,100,0), (255,255,100), (255,255,255)],
        )
        default = (0, 0, 0)

        summary = self.connectivity_summary(cre_type=None)

        shape = (len(rows), len(cols))
        text = np.empty(shape, dtype=object)
        fgcolor = np.empty(shape, dtype=object)
        bgcolor = np.empty(shape, dtype=object)

        for i,row in enumerate(rows):
            for j,col in enumerate(cols):
                if (row, col) in summary:
                    conn = summary[(row, col)]['connected']
                    uconn = summary[(row, col)]['unconnected']
                    color = colormap.map(conn/(conn + uconn))
                else:
                    conn, uconn = 0, 0
                    color = default
                bgcolor[i, j] = color
                text[i, j] = "%d/%d" % (conn, conn+uconn)
                fgcolor[i, j] = 'w' if sum(color) < 300 else 'k'
                if conn == uconn == 0:
                    fgcolor[i, j] = 0.3

        w.matrix = MatrixItem(text=text, fgcolor=fgcolor, bgcolor=bgcolor,
                              rows=rows, cols=cols, size=size)
        v.addItem(w.matrix)

        # colormap is logarithmic; remap to linear for legend
        colors = colormap.color
        x = np.linspace(0, 1, len(colors))
        cmap2 = pg.ColorMap(x, colors)
        legend = pg.GradientLegend([25, 300], [-20, -30])
        legend.setGradient(cmap2.getGradient())
        legend.setLabels({'%d'%int(a*100):b for a,b in zip(colormap.pos, x)})
        v.addItem(legend)
        w.show()
        self.matrix_widget = w

    def n_connections_probed(self):
        """Return (total_probed, total_connected) for all experiments in this list.
        """
        tot_probed = 0
        tot_connected = 0
        for expt in self:
            tot_probed += expt.n_connections_probed
            tot_connected += expt.n_connections
        return tot_probed, tot_connected

    def connection_stim_summary(self):
        """Return a structure that contains stimulus summary information for each connection type.

            {(pre_type, post_type): {(clamp_mode, freq, holding): [n1_sweeps, n2_sweeps,...]}}

        """
        conn_info = self.connection_summary(list_stims=True)
        connection_sweep_summary = {}
        for conn in conn_info:
            c1, c2 = conn["cells"]
            connection_type = (c1.cre_type, c2.cre_type)
            conn_type_info = connection_sweep_summary.setdefault(connection_type, {})
            for stim, n_sweeps in conn["stims"].items():
                conn_type_info.setdefault(stim, [])
                conn_type_info[stim].append(sum(n_sweeps))

        return connection_sweep_summary

    def print_expt_summary(self, list_stims=False):
        fields = ['# probed', '# connected', 'age', 'cre types']
        if list_stims:
            fields.append('stim sets')
        print("----------------------------------------------------------")
        print("  Experiment Summary  (%s)" % ', '.join(fields))
        print("----------------------------------------------------------")

        if len(self.start_skip) > 0:
            print("[ skipped %d earlier experiments ]" % len(self.start_skip))
        tot_probed = 0
        tot_connected = 0
        ages = []
        for i,expt in enumerate(self):
            n_p = expt.n_connections_probed
            n_c = expt.n_connections
            tot_probed += n_p
            tot_connected += n_c
            ages.append(expt.age)

            fmt = "%s: %s %s %s %s %s"
            fmt_args = [str(expt.summary_id).rjust(4), str(n_p).ljust(5), str(n_c).ljust(5), str(expt.age).ljust(7), ', '.join(expt.cre_types).ljust(15), ':'.join(expt.expt_id)]

            # get list of stimuli
            if list_stims:
                stims = expt.list_stims()

                fmt += "\t%s"
                fmt_args.append(', '.join(stims))

            print(fmt % tuple(fmt_args))

        if len(self.stop_skip) > 0:
            print("[ skipped %d later experiments ]" % len(self.stop_skip))
        print("")

        print("Mean age: %0.1f" % np.mean(ages))
        print("")

    def connectivity_summary(self, cre_type):
        summary = {}
        for expt in self:
            for k,v in expt.summary().items():
                if cre_type is not None and cre_type != list(k):
                    continue
                if k not in summary:
                    summary[k] = {'connected':0, 'unconnected':0, 'cdist':[], 'udist':[]}
                summary[k]['connected'] += v['connected']
                summary[k]['unconnected'] += v['unconnected']
                summary[k]['cdist'].extend(v['cdist'])
                summary[k]['udist'].extend(v['udist'])
        return summary

    def print_connectivity_summary(self, cre_type=None):
        print("-------------------------------------------------------------")
        print("     Connectivity  (# connected/probed, % connectivity, %250, %100, cdist, udist, adist)")
        print("-------------------------------------------------------------")

        tot_probed, tot_connected = self.n_connections_probed()

        summary = self.connectivity_summary(cre_type)

        with warnings.catch_warnings():  # we expect warnings when nanmean is called on an empty list
            warnings.simplefilter("ignore")
            totals = []
            for k,v in summary.items():
                probed = v['connected'] + v['unconnected']
                
                # calculate probability of connectivity over all points,
                # within 250um, and within 100um.
                pconn = []
                for max_dist in (1e9, 250e-6, 100e-6):
                    c = sum(np.array(v['cdist']) <= max_dist)
                    t = c + np.sum(np.array(v['udist']) <= max_dist)
                    pconn.append(c / t)
                
                totals.append((
                    k[0],                        # pre type
                    k[1],                        # post type
                    v['connected'],              # n connected
                    probed,                      # n probed
                    100*pconn[0],                # % connected
                    100*pconn[1],                # % connected <= 250um
                    100*pconn[2],                # % connected <= 100um
                    np.nanmean(v['cdist'])*1e6,  # avg cdist
                    np.nanmean(v['udist'])*1e6,  # avg udist
                    np.nanmean(v['cdist']+v['udist'])*1e6   # avg dist
                ))

        colsize = max([len(t[0]) + len(t[1]) for t in totals]) + 8
        totals.sort(key=lambda x: (x[4], x[3], x[0], x[1]), reverse=True)
        for tot in totals:
            pad = " " * (colsize - (len(tot[0]) + len(tot[1]) + 3))
            fields = list(tot)
            fields.insert(2, pad)
            fields = tuple(fields)
            try:
                print(u"%s → %s%s\t:\t%d/%d\t%0.2f%%\t%0.2f%%\t%0.2f%%\t%0.2f\t%0.2f\t%0.2f" % fields)
            except UnicodeEncodeError:
                print("%s - %s%s\t:\t%d/%d\t%0.2f%%\t%0.2f%%\t%0.2f%%\t%0.2f\t%0.2f\t%0.2f" % fields)

        print("\nTotal:  \t%d/%d\t%0.2f%%" % (tot_connected, tot_probed, 100*tot_connected/(tot_connected+tot_probed)))
        print("")

    def print_label_summary(self):
        print("-----------------------")
        print("       Labeling")
        print("-----------------------")

        n_qc_passed = 0
        n_dye_passed = 0
        n_qc_and_biocytin_passed = 0
        n_dye_and_biocytin_passed = 0

        for expt in self:
            for cell in expt.cells.values():
                biocytin = cell.labels.get('biocytin', None)
                if biocytin is None:
                    # ignore cells with no biocytin data
                    continue
                biocytin = (biocytin == '+')
                if cell.pass_qc:
                    n_qc_passed += 1
                    if biocytin:
                        n_qc_and_biocytin_passed += 1
                if cell.cre_type is not None:
                    n_dye_passed += 1
                    if biocytin:
                        n_dye_and_biocytin_passed += 1

        dye_biocytin_percent = 100*n_dye_and_biocytin_passed/n_dye_passed if n_dye_passed > 0 else 0
        print("%0.2f (%d/%d) of dye-filled cells had a biocytin fill" % (dye_biocytin_percent, n_dye_and_biocytin_passed, n_dye_passed))

        qc_biocytin_percent = 100*n_qc_and_biocytin_passed/n_qc_passed if n_qc_passed > 0 else 0
        print("%0.2f (%d/%d) of qc-passed cells had a biocytin fill" % (qc_biocytin_percent, n_qc_and_biocytin_passed, n_qc_passed))

        print("")

    def connection_summary(self, cre_type, list_stims=False):
        """Return a structure that contains summary information for each connection found.

            [{'cells': (pre, post), 'expt': expt}, ...]

        If *list_stims* is True, then each connection dict also includes a 'stims' key:

            'stims': {(clamp_mode, stim_name, holding): [n_sweeps, S_n_sweeps]}
        """
        summary = []
        for expt in self:
            for pre_id, post_id in expt.connections:

                c1, c2 = expt.cells[pre_id], expt.cells[post_id]
                if cre_type is not None and cre_type != [c1.cre_type, c2.cre_type]:
                    continue
                conn_info = {'cells': (c1, c2), 'expt': expt}
                summary.append(conn_info)

                if list_stims:
                    stims = {}
                    for sweep in expt.sweep_summary:
                        # NOTE the -1 here converts from cell ID to headstage ID.
                        # Eventually this mapping should be recorded explicitly.
                        info1 = sweep.get(pre_id - 1)
                        info2 = sweep.get(post_id - 1)

                        if info1 is None or info2 is None:
                            continue
                        stim_name = expt._short_stim_name(info1[0])
                        if stim_name.upper().startswith('S'):
                            short_pulse = True
                            stim_name = stim_name[1:]
                        else:
                            short_pulse = False
                        mode = info2[1]
                        holding = 5 * np.round(info2[3] * 1000 / 5.0)
                        stim = (mode, stim_name, int(holding))
                        stims.setdefault(stim,[0,0])
                        if short_pulse is True:
                            stims[stim][1] += 1
                        else:
                            stims[stim][0] += 1
                    conn_info['stims'] = stims
        return summary

    def print_connection_summary(self, cre_type=None, list_stims=False):
        print("-----------------------")
        print("       Connections")
        print("-----------------------")
        conns = self.connection_summary(cre_type, list_stims=list_stims)
        for conn in conns:
            c1, c2 = conn['cells']
            distance = (c1.distance(c2))*10**6
            expt = conn['expt']
            i = self._expts.index(expt)
            if 'stims' in conn:
                print(u"%d %d->%d: \t%s -> %s; %.0f um\t%s" % (i, c1.cell_id, c2.cell_id, c1.cre_type, c2.cre_type, distance, expt.expt_id))
                stims = conn['stims']
                if len(stims) == 0:
                    print('no sweeps: %d %d\n' % (c1.cell_id, c2.cell_id))
                    import pprint
                    pprint.pprint(expt.sweep_summary)

                else:
                    stims = '\n'.join(["%s %s %dmV; %d,%d sweeps"% (s+(n[0],n[1])) for s,n in stims.items()])
                    print(stims)
            else:
                print(u"%d %d->%d: \t%s -> %s; %.0f um\t%s" % (i, c1.cell_id, c2.cell_id, c1.cre_type, c2.cre_type, distance, expt.expt_id))

        print("")

    def print_connection_sweep_summary(self, sweep_threshold=[5,10]):
        from collections import OrderedDict
        print("-----------------------")
        print("  Connection: connected/total probed ")
        print("            Stimulus Set: # connections w/ >= %d (induction) and %d (recovery) sweeps" % (sweep_threshold[0], sweep_threshold[1]))
        print("-----------------------")
        connection_sweep_summary = self.connection_stim_summary()
        connection_types = connection_sweep_summary.keys()
        summary = self.connectivity_summary()
        for connection_type in connection_types:
            connected = summary[connection_type]['connected']
            probed = connected + summary[connection_type]['unconnected']
            print("\n%s->%s: %d/%d" % (connection_type[0], connection_type[1], connected, probed))
            stim_sets = connection_sweep_summary[connection_type].keys()
            stim_sets = sorted(stim_sets, key = lambda s:(s[0], int(s[1].split('H')[0]) if s[1].split('H')[0].isdigit() else s[1], -s[2]))
            stim_summary = OrderedDict()
            for stim_set in stim_sets:
                if 'recovery' in stim_set:
                    threshold = sweep_threshold[1]
                else:
                    threshold = sweep_threshold[0]
                num_connections = sum(connections >= threshold for connections in
                                          connection_sweep_summary[connection_type][stim_set])
                stim_summary.setdefault(stim_set, 0)
                stim_summary[stim_set] += num_connections
            for stim_set in stim_summary:
                n_connections = 0
                if connection_type[0] in INHIBITORY_CRE_TYPES:
                    if stim_set[2] <= -50 and stim_set[2] >= -60:
                        n_connections = stim_summary[stim_set]
                elif connection_type[0] in EXCITATORY_CRE_TYPES:
                    if stim_set[2] <= -65 and stim_set[2] >= -75:
                        n_connections = stim_summary[stim_set]
                else:
                    n_connections = stim_summary[stim_set]
                if n_connections:
                    print("\t%s:\t%d" % (' '.join([str(s) for s in stim_set]), n_connections))