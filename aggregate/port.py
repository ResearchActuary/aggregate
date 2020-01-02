"""
Purpose
-------

A Portfolio represents a collection of Aggregate objects. Applications include

* Model a book of insurance
* Model a large account with several sub lines
* Model a reinsurance portfolio or large treaty



"""

import collections
import json
import logging
from copy import deepcopy

import matplotlib.cm as cm
import matplotlib.pyplot as plt
import matplotlib
import numpy as np
import pandas as pd
import pypandoc
import scipy.stats as ss
from IPython.core.display import HTML, display
from matplotlib.ticker import MultipleLocator, StrMethodFormatter, MaxNLocator, FixedLocator, \
    FixedFormatter, AutoMinorLocator
from scipy import interpolate

from .distr import Aggregate
from .distr import Severity
from .spectral import Distortion
from .utils import ft, \
    ift, sln_fit, sgamma_fit, \
    axiter_factory, AxisManager, html_title, \
    sensible_jump, suptitle_and_tight, \
    MomentAggregator, \
    Answer

# fontsize : int or float or {'xx-small', 'x-small', 'small', 'medium', 'large', 'x-large', 'xx-large'}
matplotlib.rcParams['legend.fontsize'] = 'xx-small'

logger = logging.getLogger('aggregate')
# use this one to broadcast to the stderr
dev_logger = logging.getLogger('aggregate.dev')


# debug
# info
# warning
# error
# critical

class Portfolio(object):
    """
    Portfolio creates and manages a portfolio of Aggregate objects.

    :param name: the name of the portfolio, no spaces or underscores
    :param spec_list: a list of 1) dictionary: Aggregate object dictionary specifications or
                                2) Aggregate: An actual aggregate objects or
                                3) tuple (type, dict) as returned by uw['name'] or
                                4) string: Names referencing objects in the optionally passed underwriter

    """

    def __init__(self, name, spec_list, uw=None):
        self.name = name
        self.agg_list = []
        self.line_names = []
        logger.info(f'Portfolio.__init__| creating new Portfolio {self.name} at {super(Portfolio, self).__repr__()}')
        ma = MomentAggregator()
        max_limit = 0
        for spec in spec_list:
            if isinstance(spec, Aggregate):
                # directly passed in an agg object
                a = spec
                agg_name = spec.name
            elif isinstance(spec, str):
                # look up object in uw return actual instance
                # note here you could do uw.aggregate[spec] and get the dictionary def
                # or uw(spec) to return the already-created (and maybe updated) object
                # we go the latter route...if user wants they can pull off the dict item themselves
                if uw is None:
                    raise ValueError(f'Must pass valid Underwriter instance to create aggs by name')
                try:
                    a = uw(spec)
                except e:
                    print(f'Item {spec} not found in your underwriter')
                    raise e
                agg_name = a.name
            elif isinstance(spec, tuple):
                # uw returns type, spec
                assert spec[0] == 'agg'
                a = Aggregate(**spec[1])
                agg_name = spec[1]['name']
            elif isinstance(spec, dict):
                a = Aggregate(**spec)
                agg_name = spec['name'][0] if isinstance(spec['name'], list) else spec['name']
            else:
                raise ValueError(f'Invalid type {type(spec)} passed to Portfolio, expect Aggregate, str or dict.')

            self.agg_list.append(a)
            self.line_names.append(agg_name)
            self.__setattr__(agg_name, a)
            ma.add_fs(a.report_ser[('freq', 'ex1')], a.report_ser[('freq', 'ex2')], a.report_ser[('freq', 'ex3')],
                      a.report_ser[('sev', 'ex1')], a.report_ser[('sev', 'ex2')], a.report_ser[('sev', 'ex3')])
            max_limit = max(max_limit, np.max(np.array(a.limit)))
        self.line_names_ex = self.line_names + ['total']
        for n in self.line_names:
            # line names cannot equal total
            if n == 'total':
                raise ValueError('Line names cannot equal total, it is reserved for...total')
        # make a pandas data frame of all the statistics_df
        temp_report = pd.concat([a.report_ser for a in self.agg_list], axis=1)

        # max_limit = np.inf # np.max([np.max(a.get('limit', np.inf)) for a in spec_list])
        temp = pd.DataFrame(ma.stats_series('total', max_limit, 0.999, remix=False))
        self.statistics_df = pd.concat([temp_report, temp], axis=1)
        # future storage
        self.density_df = None
        self.epd_2_assets = {}
        self.assets_2_epd = {}
        self.priority_capital_df = None
        self.priority_analysis_df = None
        self.audit_df = None
        self.padding = 0
        self.tilt_amount = 0
        self._linear_quantile_function = None
        self._cdf = None
        self._pdf = None
        self._tail_var = None
        self.bs = 0
        self.log2 = 0
        self.ex = 0
        self.last_update = 0
        self.hash_rep_at_last_update = ''
        self.last_distortion = None
        self.last_sev_calc = ''
        self.last_remove_fuzz = 0
        self.approx_type = ""
        self.approx_freq_ge = 0
        self.q_temp = None  # for storing the info about the quantile function

    def __str__(self):
        """
        Goal: readability
        :return:
        """
        # cannot use ex, etc. because object may not have been updated
        if self.audit_df is None:
            ex = self.statistics_df.loc[('agg', 'mean'), 'total']
            empex = np.nan
            isupdated = False
        else:
            ex = self.get_stat(stat="Mean")
            empex = self.get_stat()
            isupdated = True
        # df = pd.DataFrame(columns=['Statistic', 'Value'])
        # df = df.set_index('Statistic')
        # df.loc['Portfolio Name', 'Value'] = self.name
        # df.loc['Expected loss', 'Value'] = ex
        # df.loc['Model loss', 'Value'] = empex
        # df.loc['Error', 'Value'] = ex / empex - 1
        # print(df)
        s = f'Portfolio name           {self.name:<15s}\n' \
            f'Theoretic expected loss  {ex:15,.1f}\n' \
            f'Actual expected loss     {empex:15,.1f}\n' \
            f'Error                    {empex / ex - 1:15.6f}\n' \
            f'Discretization size      {self.log2:15d}\n' \
            f'Bucket size              {self.bs:15.2f}\n' \
            f'{object.__repr__(self)}'
        if not isupdated:
            s += '\nNOT UPDATED!'
        return s

    def __repr__(self):
        """
        Goal unmbiguous
        :return:
        """
        # return str(self.to_dict())

        s = [super(Portfolio, self).__repr__(), f"{{ 'name': '{self.name}'"]
        agg_list = [str({k: v for k, v in a.__dict__.items() if k in Aggregate.aggregate_keys})
                    for a in self.agg_list]
        s.append(f"'spec': [{', '.join(agg_list)}]")
        if self.bs > 0:
            s.append(f'"bs": {self.bs}')
            s.append(f'"log2": {self.log2}')
            s.append(f'"padding": {self.padding}')
            s.append(f'"tilt_amount": {self.tilt_amount}')
            s.append(f'"last_distortion": "{self.last_distortion.__repr__()}"')
            s.append(f'"last_sev_calc": "{self.last_sev_calc}"')
            s.append(f'"remove_fuzz": {self.last_remove_fuzz}')
            s.append(f'"approx_type": "{self.approx_type}"')
            s.append(f'"approx_freq_ge": {self.approx_freq_ge}')
        return ', '.join(s) + '}'

    def _repr_html_(self):
        s = [f'<h2>Portfolio object: {self.name}</h2>']
        _n = len(self.agg_list)
        _s = "" if _n <= 1 else "s"
        s.append(f'Portfolio contains {_n} aggregate component{_s}')
        summary_sl = (slice(None), ['mean', 'cv', 'skew'])
        if self.audit_df is not None:
            # _df = self.audit_df[['Mean', 'EmpMean', 'MeanErr', 'CV', 'EmpCV', 'CVErr', 'P99.0']]
            # another option TODO consider
            _df = pd.concat((self.statistics_df.loc[summary_sl, :],
                             self.audit_df[['Mean', 'EmpMean', 'MeanErr', 'CV', 'EmpCV', 'CVErr', 'P99.0']].T),
                            sort=True)
            s.append(_df._repr_html_())
        else:
            s.append(self.statistics_df.loc[summary_sl, :]._repr_html_())
        return '\n'.join(s)

    def __hash__(self):
        """
        hashging behavior
        :return:
        """
        # TODO fix
        # return hash(self.__repr__())
        return hash(repr(self.__dict__))

    def __iter__(self):
        """
        make Portfolio iterable: for each x in Portfolio

        :return:
        """
        return iter(self.agg_list)

    def __getitem__(self, item):
        """
        alloow Portfolio[slice] to return bits of agg_list

        :param item:
        :return:
        """
        return self.agg_list[item]

    def json(self, stream=None):
        """
        write object as json

        :param    stream:
        :return:  stream or text
        """

        args = dict()
        # TODO fix is it bs or bs!!
        args["bs"] = self.bs
        args["log2"] = self.log2
        args["padding"] = self.padding
        args["tilt_amount"] = self.tilt_amount
        args["last_distortion"] = self.last_distortion.__repr__()
        args["last_sev_calc"] = self.last_sev_calc
        args["remove_fuzz"] = self.last_remove_fuzz
        args["approx_type"] = self.approx_type
        args["approx_freq_ge"] = self.approx_freq_ge
        args["last_update"] = str(self.last_update)
        args["hash_rep_at_last_update"] = str(self.hash_rep_at_last_update)

        d = dict()
        # original
        # d[self.name] = dict(args=args, spec=[a.spec for a in self.agg_list])
        d['name'] = self.name
        d['args'] = args
        d['spec_list'] = [a._spec for a in self.agg_list]

        logger.info(f'Portfolio.json| dummping {self.name} to {stream}')
        s = json.dumps(d)  # , default_flow_style=False, indent=4)
        logger.debug(f'Portfolio.json | {s}')
        if stream is None:
            return s
        else:
            return stream.write(s)

    def save(self, filename='', mode='a'):
        """
        persist to json in filename; if none save to user.json

        TODO: update user list in Examples?

        :param filename:
        :param mode: for file open
        :return:
        """
        if filename == "":
            # TODO: directory naming
            filename = './agg/user.json'

        with open(filename, mode=mode) as f:
            self.json(stream=f)
            logger.info(f'Portfolio.save | {self.name} saved to {filename}')

    def __add__(self, other):
        """
        Add two portfolio objets INDEPENDENT sum (down road can look for the same severity...)

        TODO same severity!

        :param other:
        :return:
        """
        assert isinstance(other, Portfolio)
        # TODO consider if better naming of L&R sides is in order
        new_spec = []
        for a in self.agg_list:
            c = deepcopy(a._spec)
            c['name'] = c['name']
            new_spec.append(c)
        for a in other.agg_list:
            c = deepcopy(a._spec)
            c['name'] = c['name']
            new_spec.append(c)

        return Portfolio(f'({self.name}) + ({other.name})', new_spec)

    def __rmul__(self, other):
        """
        new = other * self; treat as scale change

        :param other:
        :return:
        """

        assert other > 0

        new_spec = []
        for a in self.agg_list:
            new_spec.append(deepcopy(a._spec))

        for d in new_spec:
            # d is a dictionary agg spec, need to adjust the severity
            s = d['severity']
            if 'mean' in s:
                s['mean'] *= other
            elif 'scale' in s:
                s['scale'] *= other
            else:
                raise ValueError(f"Cannot adjust s['name'] for scale")

        return Portfolio(f'{other} x {self.name}', new_spec)

    def __mul__(self, other):
        """
        new = self * other, other integer, sum of other independent copies

        :param other:
        :return:
        """

        assert isinstance(other, int)

        new_spec = []
        for a in self.agg_list:
            new_spec.append(deepcopy(a._spec))

        for d in new_spec:
            # d is a dictionary agg spec, need to adjust the frequency
            # TODO better freq dists; deal with Bernoulli where n=log<1
            d['frequency']['n'] *= other

        return Portfolio(f'Sum of {other} copies of {self.name}', new_spec)

    def get_stat(self, line='total', stat='EmpMean'):
        """
        Other analysis suggests that iloc and iat are about same speed but slower than ix

        :param line:
        :param stat:
        :return:
        """
        return self.audit_df.loc[line, stat]

    def q(self, p, kind='lower'):
        """
        return lowest quantile, appropriate for discrete bucketing.
        quantile guaranteed to be in the index
        nearest does not work because you always want to pick rounding up

        Definition 2.1 (Quantiles)
        x(α) = qα(X) = inf{x ∈ R : P[X ≤ x] ≥ α} is the lower α-quantile of X
        x(α) = qα(X) = inf{x ∈ R : P[X ≤ x] > α} is the upper α-quantile of X.

        We use the x-notation if the dependence on X is evident, otherwise the q-notion.
        Acerbi and Tasche (2002)

        :param p:
        :param kind: allow upper or lower quantiles
        :return:
        """
        if self._linear_quantile_function is None:
            # revised Dec 2019
            self._linear_quantile_function = {}
            self.q_temp = self.density_df[['loss', 'F']].groupby('F').agg({'loss': np.min})
            self.q_temp.loc[1, 'loss'] = self.q_temp.loss.iloc[-1]
            self.q_temp.loc[0, 'loss'] = 0
            self.q_temp = self.q_temp.sort_index()
            # that q_temp left cts, want right continuous:
            self.q_temp['loss_s'] = self.q_temp.loss.shift(-1)
            self.q_temp.iloc[-1, 1] = self.q_temp.iloc[-1, 0]
            # previously, haha
            # self._linear_quantile_function['upper'] = \
            #     interpolate.interp1d(self.q_temp.index, self.q_temp.loss_s, kind='nearest', bounds_error=False,
            #                          fill_value='extrapolate')
            self._linear_quantile_function['upper'] = \
                interpolate.interp1d(self.q_temp.index, self.q_temp.loss_s, kind='previous', bounds_error=False,
                                     fill_value='extrapolate')
            self._linear_quantile_function['lower'] = \
                interpolate.interp1d(self.q_temp.index, self.q_temp.loss, kind='previous', bounds_error=False,
                                     fill_value='extrapolate')
            # original
            # self._linear_quantile_function = \
            #     interpolate.interp1d(self.density_df.F, self.density_df.loss, kind='nearest', bounds_error=False,
            #                          fill_value='extrapolate')


        l = float(self._linear_quantile_function[kind](p))
        # because we are not interpolating the returned value must (should) be in the index...
        assert l in self.density_df.index
        return l
        # find next nearest index value if not an exact match (this is slightly faster and more robust
        # than l/bs related math)
        # l1 = self.density_df.index.get_loc(l, 'bfill')
        # l1 = self.density_df.index[l1]
        # return l1

    # def quantile_function(self, kind='linear'):
    #     """
    #     return an approximation to the quantile function
    #     linear is approximation for a continuous (uniform) version of bucketing
    #     linear does not return an item in the index and is not correct for the discrete bucketing
    #
    #     TODO sort out...this isn't right
    #
    #     :param kind:
    #     :return:
    #     """
    #     q = interpolate.interp1d(self.density_df.F, self.density_df.loss, kind=kind,
    #                              bounds_error=False, fill_value='extrapolate')
    #     return q

    def cdf(self, x):
        """
        distribution function

        :param x:
        :return:
        """
        if self._cdf is None:
                # Dec 2019: kind='linear' --> kind='previous'
            self._cdf = interpolate.interp1d(self.density_df.loss, self.density_df.F, kind='previous',
                                             bounds_error=False, fill_value='extrapolate')
        return self._cdf(x)

    def sf(self, x):
        """
        survival function

        :param x:
        :return:
        """
        return 1 - self.cdf(x)

    def pdf(self, x):
        """
        probability density function, assuming a continuous approximation of the bucketed density
        :param x:
        :return:
        """
        if self._pdf is None:
            self._pdf = interpolate.interp1d(self.density_df.loss, self.density_df.p_total, kind='linear',
                                             bounds_error=False, fill_value='extrapolate')
        return self._pdf(x) / self.bs

    # # make some handy aliases; delete these go strictly with scipy.stats notation
    # def F(self, x):
    #     """
    #     handy alias for distribution, CDF
    #     :param x:
    #     :return:
    #     """
    #     return self.cdf(x)
    #
    # def S(self, x):
    #     """
    #     handy alias for survival function, S
    #     :param x:
    #     :return:
    #     """
    #     return self.sf(x)

    def var(self, p):
        """
        value at risk = alias for quantile function

        :param p:
        :return:
        """
        return self.q(p)

    def tvar(self, p):
        """
        Compute the tail value at risk at threshold p

        Definition 2.6 (Tail mean and Expected Shortfall)
        Assume E[X−] < ∞. Then
        x¯(α) = TM_α(X) = α^{−1}E[X 1{X≤x(α)}] + x(α) (α − P[X ≤ x(α)])
        is α-tail mean at level α the of X.
        Acerbi and Tasche (2002)

        We are interested in the right hand exceedence [?? note > vs ≥]
        α^{−1}E[X 1{X > x(α)}] + x(α) (P[X ≤ x(α)] − α)

        McNeil etc. p66-70 - this follows from def of ES as an integral
        of the quantile function


        :param p:
        :return:
        """
        assert self.density_df is not None

        _var = self.q(p)
        # evil floating point issue here... this is XXXX TODO kludge because 13 is not generally applicable
        ex = self.density_df.loc[np.round(_var + self.bs, 13):, ['p_total', 'loss']].product(axis=1).sum()
        pip = (self.density_df.loc[_var, 'F'] - p) * _var
        t_var = 1 / (1 - p) * (ex + pip)
        return t_var

        # original implementation interpolated
        # if self._tail_var is None:
        #     # make tvar function
        #     self._tail_var = interpolate.interp1d(self.density_df.F, self.density_df.exgta_total,
        #                                           kind='linear', bounds_error=False,
        #                                           fill_value='extrapolate')
        # return self._tail_var(p)

    def tvar_threshold(self, p):
        """
        Find the value pt such that TVaR(pt) = VaR(p) using numerical Newton Raphson
        """
        a = self.q(p)

        def f(p):
            return self.tvar(p) - a

        loop = 0
        p1 = 1 - 2 * (1 - p)
        fp1 = f(p1)
        delta = 1e-5
        while abs(fp1) > 1e-6 and loop < 10:
            df1 = (f(p1 + delta) - fp1) / delta
            p1 = p1 - fp1 / df1
            fp1 = f(p1)
            loop += 1
        if loop == 10:
            raise ValueError(f'Difficulty computing TVaR to match VaR at p={p}')
        return p1

    def equal_risk_var_tvar(self, p, p_t):
        """
        solve for equal risk var and tvar: find pv and pt such that sum of
        individual line VaR/TVaR at pv/pt equals the VaR(p) or TVaR(p_t)
        """
        # these two should obviously be the same
        target_v = self.q(p)
        target_t = self.tvar(p_t)

        def fv(p):
            return sum([float(a.middle_q(p)) for a in self]) - target_v

        def ft(p):
            return sum([float(a.tvar(p)) for a in self]) - target_t

        ans = np.zeros(2)
        for i, f in enumerate([fv, ft]):
            p1 = 1 - 2 * (1 - (p if i == 0 else p_t))
            fp1 = f(p1)
            loop = 0
            delta = 1e-5
            while abs(fp1) > 1e-6 and loop < 10:
                dfp1 = (f(p1 + delta) - fp1) / delta
                p1 = p1 - fp1 / dfp1
                fp1 = f(p1)
                loop += 1
            if loop == 10:
                raise ValueError(f'Trouble finding equal risk {"TVaR" if i else "VaR"} at p={p}')
            ans[i] = p1
        return ans

    def merton_perold(self, p):
        """
        compute Merton Perold capital allocation at T/VaR(p) capital using VaR as risk measure
        v = q(p)
        TODO? Add TVaR MERPER
        """
        # figure total assets
        a = self.q(p)
        # shorthand abbreviation
        df = self.density_df
        loss = df.loss
        ans = []
        total = 0
        for l in self.line_names:
            F = df[f'ημ_{l}'].cumsum()
            f = interpolate.interp1d(F, loss)
            _ = a - f(p)
            ans.append(_)
            total += _
        ans.append(total)
        return ans

    def as_severity(self, limit=np.inf, attachment=0, conditional=False):
        """
        convert into a severity without recomputing

        throws error if self not updated

        :param limit:
        :param attachment:
        :param conditional:
        :return:
        """
        if self.density_df is None:
            raise ValueError('Must update prior to converting to severity')
        return Severity(sev_name=self, sev_a=self.log2, sev_b=self.bs,
                        exp_attachment=attachment, exp_limit=limit, conditional=conditional)

    def fit(self, approx_type='slognorm', output='agg'):
        """
        returns a dictionary specification of the portfolio aggregate_project
        if updated uses empirical moments, otherwise uses theoretic moments

        :param approx_type: slognorm | sgamma
        :param output: return a dict or agg language specification
        :return:
        """
        if self.audit_df is None:
            # not updated
            m = self.statistics_df.loc[('agg', 'mean'), 'total']
            cv = self.statistics_df.loc[('agg', 'cv'), 'total']
            skew = self.statistics_df.loc[('agg', 'skew'), 'total']
        else:
            # use statistics_df matched to computed aggregate_project
            m, cv, skew = self.audit_df.loc['total', ['EmpMean', 'EmpCV', 'EmpSkew']]

        name = f'{approx_type[0:4]}~{self.name[0:5]}'
        agg_str = f'agg {name} 1 claim sev '

        if approx_type == 'slognorm':
            shift, mu, sigma = sln_fit(m, cv, skew)
            # self.fzapprox = ss.lognorm(sigma, scale=np.exp(mu), loc=shift)
            sev = {'sev_name': 'lognorm', 'sev_shape': sigma, 'sev_scale': np.exp(mu), 'sev_loc': shift}
            agg_str += f'{np.exp(mu)} * lognorm {sigma} + {shift} '
        elif approx_type == 'sgamma':
            shift, alpha, theta = sgamma_fit(m, cv, skew)
            # self.fzapprox = ss.gamma(alpha, scale=theta, loc=shift)
            sev = {'sev_name': 'gamma', 'sev_a': alpha, 'sev_scale': theta, 'sev_loc': shift}
            agg_str += f'{theta} * lognorm {alpha} + {shift} '
        else:
            raise ValueError(f'Inadmissible approx_type {approx_type} passed to fit')

        if output == 'agg':
            agg_str += ' fixed'
            return agg_str
        else:
            return {'name': name, 'note': f'frozen version of {self.name}', 'exp_en': 1, **sev, 'freq_name': 'fixed'}

    def collapse(self, approx_type='slognorm'):
        """
        returns new Portfolio with the fit

        deprecated...prefer uw(self.fit()) to go through the agg language approach

        TODO: make a version returning an rv_histogram


        :param approx_type: slognorm | sgamma
        :return:
        """
        spec = self.fit(approx_type, output='dict')
        logger.debug(f'Portfolio.collapse | Collapse created new Portfolio with spec {spec}')
        return Portfolio(f'Collapsed {self.name}', [spec])

    def percentiles(self, pvalues=None):
        """
        report_ser on percentiles and large losses
        uses interpolation, audit_df uses nearest

        :pvalues: optional vector of log values to use. If None sensible defaults provided
        :return: DataFrame of percentiles indexed by line and log
        """
        df = pd.DataFrame(columns=['line', 'log', 'Agg Quantile'])
        df = df.set_index(['line', 'log'])
        # df.columns.name = 'perspective'
        if pvalues is None:
            pvalues = [0.5, 0.75, 0.8, 0.85, 0.9, 0.95, 0.98, 0.99, 0.994, 0.995, 0.999, 0.9999]
        for line in self.line_names_ex:
            q_agg = interpolate.interp1d(self.density_df.loc[:, f'p_{line}'].cumsum(), self.density_df.loss,
                                         kind='linear', bounds_error=False, fill_value='extrapolate')
            for p in pvalues:
                qq = q_agg(p)
                df.loc[(line, p), :] = [float(qq)]
        df = df.unstack(level=1)
        return df

    def recommend_bucket(self):
        """
        data to help estimate a good bucket size

        :return:
        """
        df = pd.DataFrame(columns=['line', 'bs10'])
        df = df.set_index('line')
        for a in self.agg_list:
            df.loc[a.name, :] = [a.recommend_bucket(10)]
        df['bs11'] = df['bs10'] / 2
        df['bs12'] = df['bs10'] / 4
        df['bs13'] = df['bs10'] / 8
        df['bs14'] = df['bs10'] / 16
        df['bs15'] = df['bs10'] / 32
        df['bs16'] = df['bs10'] / 64
        df['bs17'] = df['bs10'] / 128
        df['bs18'] = df['bs10'] / 256
        df['bs19'] = df['bs10'] / 515
        df['bs20'] = df['bs10'] / 1024
        df.loc['total', :] = df.sum()
        return df

    def update(self, log2, bs, approx_freq_ge=100, approx_type='slognorm', remove_fuzz=False,
               sev_calc='discrete', discretization_calc='survival', padding=1, tilt_amount=0, epds=None,
               trim_df=True, verbose=False, add_exa=True):
        """
        create density_df, performs convolution. optionally adds additional information if ``add_exa=True``
        for allocation and priority analysis

        :param log2:
        :param bs: bucket size
        :param approx_freq_ge: use method of moments if frequency is larger than ``approx_freq_ge``
        :param approx_type: type of method of moments approx to use (slognorm or sgamma)
        :param remove_fuzz: remove machine noise elements from FFT
        :param sev_calc: how to calculate the severity, discrete (point masses as xs) or continuous (uniform between xs points)
        :param discretization_calc:  survival or distribution (accurate on right or left tails)
        :param padding: for fft 1 = double, 2 = quadruple
        :param tilt_amount: for tiling methodology - see notes on density for suggested parameters
        :param epds: epd points for priority analysis; if None-> sensible defaults
        :param trim_df: remove unnecessary columns from density_df before returning
        :param verbose: level of output
        :param add_exa: run add_exa to append additional allocation information needed for pricing
        :return:
        """

        self.log2 = log2
        self.bs = bs
        self.padding = padding
        self.tilt_amount = tilt_amount
        self.approx_type = approx_type
        self.last_sev_calc = sev_calc
        self.last_remove_fuzz = remove_fuzz
        self.approx_type = approx_type
        self.approx_freq_ge = approx_freq_ge

        if self.hash_rep_at_last_update == hash(self):
            print(f'Nothing has changed since last update at {self.last_update}')
            return

        ft_line_density = {}
        line_density = {}
        not_line_density = {}

        # add the densities
        # tilting: [@Grubel1999]: Computation of Compound Distributions I: Aliasing Errors and Exponential Tilting
        # (ASTIN 1999)
        # tilt x numbuck < 20 recommented log. 210
        # num buckets and max loss from bucket size
        N = 1 << log2
        MAXL = N * bs
        xs = np.linspace(0, MAXL, N, endpoint=False)
        # make all the single line aggs
        # note: looks like duplication but will all be references
        # easier for add_exa to have as part of the portfolio module
        # tilt
        if self.tilt_amount != 0:
            tilt_vector = np.exp(self.tilt_amount * np.arange(N))
        else:
            tilt_vector = None

        ftall = None
        for agg in self.agg_list:
            nm = agg.name
            _a = agg.update(xs, self.padding, tilt_vector, 'exact' if agg.n < approx_freq_ge else approx_type,
                            sev_calc, discretization_calc, verbose=verbose)
            if verbose:
                display(_a)
            ft_line_density[nm] = agg.ftagg_density
            line_density[nm] = agg.agg_density
            if ftall is None:
                ftall = np.copy(ft_line_density[nm])
            else:
                ftall *= ft_line_density[nm]
        line_density['total'] = np.real(ift(ftall, self.padding, tilt_vector))
        ft_line_density['total'] = ftall

        # make the not self.line_density = sum of all but the given line
        # have the issue here that if you divide and the dist
        # is symmetric then you get a div zero...
        for line in self.line_names:
            ftnot = np.ones_like(ftall)
            if np.any(ft_line_density[line] == 0):
                # have to build up
                for notline in self.line_names:
                    if notline != line:
                        ftnot *= ft_line_density[notline]
            else:
                if len(self.line_names) > 1:
                    ftnot = ftall / ft_line_density[line]
            not_line_density[line] = np.real(ift(ftnot, self.padding, tilt_vector))

        # make the density_df dataframe
        d1 = {'loss': xs}
        d2 = {'p_' + i: line_density[i] for i in self.line_names_ex}
        d3 = {'ημ_' + i: not_line_density[i] for i in self.line_names}
        d = {**d1, **d2, **d3}
        self.density_df = pd.DataFrame(d, columns=d.keys(), index=xs)

        if remove_fuzz:
            logger.info(f'CPortfolio.update | Removing fuzz from {self.name}')
            eps = 2e-16
            self.density_df.loc[:, self.density_df.select_dtypes(include=['float64']).columns] = \
                self.density_df.select_dtypes(include=['float64']).applymap(lambda x: 0 if abs(x) < eps else x)

        # make audit statistics_df df
        theoretical_stats = self.statistics_df.T.filter(regex='agg')
        theoretical_stats.columns = ['EX1', 'EX2', 'EX3', 'Mean', 'CV', 'Skew', 'Limit', 'P99.9Est']
        theoretical_stats = theoretical_stats[['Mean', 'CV', 'Skew', 'Limit', 'P99.9Est']]
        percentiles = [0.9, 0.95, 0.99, 0.996, 0.999, 0.9999, 1 - 1e-6]
        self.audit_df = pd.DataFrame(
            columns=['Sum probs', 'EmpMean', 'EmpCV', 'EmpSkew', 'EmpEX1', 'EmpEX2', 'EmpEX3'] +
                    ['P' + str(100 * i) for i in percentiles])
        for col in self.line_names_ex:
            sump = np.sum(self.density_df[f'p_{col}'])
            t = self.density_df[f'p_{col}'] * self.density_df['loss']
            ex1 = np.sum(t)
            t *= self.density_df['loss']
            ex2 = np.sum(t)
            t *= self.density_df['loss']
            ex3 = np.sum(t)
            m, cv, s = MomentAggregator._moments_to_mcvsk(ex1, ex2, ex3)
            ps = np.zeros((len(percentiles)))
            temp = self.density_df[f'p_{col}'].cumsum()
            for i, p in enumerate(percentiles):
                ps[i] = (temp > p).idxmax()
            newrow = [sump, m, cv, s, ex1, ex2, ex3] + list(ps)
            self.audit_df.loc[col, :] = newrow
        self.audit_df = pd.concat((theoretical_stats, self.audit_df), axis=1, sort=True)
        self.audit_df['MeanErr'] = self.audit_df['EmpMean'] / self.audit_df['Mean'] - 1
        self.audit_df['CVErr'] = self.audit_df['EmpCV'] / self.audit_df['CV'] - 1
        self.audit_df['SkewErr'] = self.audit_df['EmpSkew'] / self.audit_df['Skew'] - 1

        # add exa details
        if add_exa:
            self._add_exa()
            # default priority analysis
            if epds is None:
                epds = np.hstack(
                    [np.linspace(0.5, 0.1, 4, endpoint=False)] +
                    [np.linspace(10 ** -n, 10 ** -(n + 1), 9, endpoint=False) for n in range(1, 7)])
                epds = np.round(epds, 7)
            self.priority_capital_df = pd.DataFrame(index=pd.Index(epds))
            for col in self.line_names:
                for i in range(3):
                    self.priority_capital_df.loc[:, '{:}_{:}'.format(col, i)] = self.epd_2_assets[(col, i)](epds)
                    self.priority_capital_df.loc[:, '{:}_{:}'.format('total', 0)] = self.epd_2_assets[('total', 0)](
                        epds)
                col = 'not ' + col
                for i in range(2):
                    self.priority_capital_df.loc[:, '{:}_{:}'.format(col, i)] = self.epd_2_assets[(col, i)](epds)
            self.priority_capital_df.loc[:, '{:}_{:}'.format('total', 0)] = self.epd_2_assets[('total', 0)](epds)
            self.priority_capital_df.columns = self.priority_capital_df.columns.str.split("_", expand=True)
            self.priority_capital_df.sort_index(axis=1, level=1, inplace=True)
            self.priority_capital_df.sort_index(axis=0, inplace=True)
        else:
            # at least want F and S to get quantile functions
            self.density_df['F'] = np.cumsum(self.density_df.p_total)
            self.density_df['S'] = 1 - self.density_df.F

        self.ex = self.audit_df.loc['total', 'EmpMean']
        self.last_update = np.datetime64('now')
        self.hash_rep_at_last_update = hash(self)
        if trim_df:
            self.trim_df()
        # invalidate stored functions
        self._linear_quantile_function = None
        self._cdf = None

    def trim_df(self):
        """
        trim out unwanted columns from density_df

        epd used in graphics

        :return:
        """
        self.density_df = self.density_df.drop(
            self.density_df.filter(regex='^e_|^exi_xlea|^[a-z_]+ημ').columns,
            axis=1
        )

    def report(self, report_list='quick'):
        """

        :param report_list:
        :return:
        """
        full_report_list = ['statistics', 'quick', 'audit', 'priority_capital', 'priority_analysis']
        if report_list == 'all':
            report_list = full_report_list
        for r in full_report_list:
            if r in report_list:
                html_title(f'{r} Report for {self.name}', 1)
                if r == 'priority_capital':
                    if self.priority_capital_df is not None:
                        display(self.priority_capital_df.loc[1e-3:1e-2, :].style)
                    else:
                        html_title(f'Report {r} not generated', 2)
                elif r == 'quick':
                    if self.audit_df is not None:
                        df = self.audit_df[['Mean', 'EmpMean', 'MeanErr', 'CV', 'EmpCV', 'CVErr', 'P99.0']]
                        display(df.style)
                    else:
                        html_title(f'Report {r} not generated', 2)
                else:
                    df = getattr(self, r + '_df', None)
                    if df is not None:
                        try:
                            display(df.style)
                        except ValueError:
                            display(df)
                    else:
                        html_title(f'Report {r} not generated', 2)

    def plot(self, kind='density', line='all', p=0.99, c=0, a=0, axiter=None, figsize=None, height=2,
             aspect=1, **kwargs):
        """
        kind = density
            simple plotting of line density or not line density;
            input single line or list of lines;
            log underscore appended as appropriate

        kind = audit
            Miscellaneous audit graphs

        kind = priority
            LEV EXA, E2Pri and combined plots by line

        kind = quick
            four bar charts of EL etc.

        kind = collateral
            plot to illustrate bivariate density of line vs not line with indicated asset a and capital c

        :param kind: density | audit | priority | quick | collateral
        :param line: lines to use, defaults to all
        :param p:   for graphics audit, x-axis scale has maximum q(p)
        :param c:   collateral amount
        :param a:   asset amount
        :param axiter: optional, pass in to use existing ``axiter``
        :param figsize: arguments passed to axis_factory if no axiter
        :param height:
        :param aspect:
        :param kwargs: passed to pandas plot routines
        :return:
        """
        do_tight = (axiter is None)

        if kind == 'quick':
            if self.audit_df is not None:
                axiter = axiter_factory(axiter, 4, figsize, height, aspect)
            else:
                axiter = axiter_factory(axiter, 3, figsize, height, aspect)

            self.statistics_df.loc[('agg', 'mean')]. \
                sort_index(ascending=True, axis=0). \
                plot(kind='bar', rot=-45, title='Expected Loss', ax=next(axiter))

            self.statistics_df.loc[('agg', 'cv')]. \
                sort_index(ascending=True, axis=0). \
                plot(kind='bar', rot=-45, title='Coeff of Variation', ax=next(axiter))

            self.statistics_df.loc[('agg', 'skew')]. \
                sort_index(ascending=True, axis=0). \
                plot(kind='bar', rot=-45, title='Skewness', ax=next(axiter))

            if self.audit_df is not None:
                self.audit_df.loc[:, 'P99.9']. \
                    sort_index(ascending=True, axis=0). \
                    plot(kind='bar', rot=-45, title='99.9th Percentile', ax=next(axiter))

        elif kind == 'density':
            if isinstance(line, str):
                if line == 'all':
                    line = [f'p_{i}' for i in self.line_names_ex]
                else:
                    line = ['p_' + line]
            elif isinstance(line, list):
                line = ['p_' + i if i[0:2] != 'ημ' else i for i in line]
            else:
                raise ValueError
            line = sorted(line)
            if 'subplots' in kwargs and len(line) > 1:
                axiter = axiter_factory(axiter, len(line), figsize, height, aspect)
                ax = axiter.grid(len(line))
            else:
                axiter = axiter_factory(axiter, 1, figsize, height, aspect)
                # want to be able to pass an axis in rather than an axiter...
                if isinstance(axiter, AxisManager):
                    ax = axiter.grid(1)
                else:
                    ax = axiter
            self.density_df.loc[:, line].sort_index(axis=1). \
                plot(sort_columns=True, ax=ax, **kwargs)
            if 'logy' in kwargs:
                _t = 'log Density'
            else:
                _t = 'Density'
            if 'subplots' in kwargs and isinstance(ax, collections.Iterable):
                for a, l in zip(ax, line):
                    a.set(title=f'{l} {_t}')
                    a.legend().set_visible(False)
            elif isinstance(ax, collections.Iterable):
                for a in ax:
                    a.set(title=f'{_t}')
            else:
                ax.set(title=_t)

        elif kind == 'audit':
            D = self.density_df
            # n_lines = len(self.line_names_ex)
            n_plots = 12  # * n_lines + 8  # assumes that not lines have been taken out!
            axiter = axiter_factory(axiter, n_plots, figsize, height, aspect)

            # make appropriate scales
            density_scale = D.filter(regex='^p_').iloc[1:, :].max().max()
            expected_loss_scale = np.sum(D.loss * D.p_total) * 1.05
            large_loss_scale = (D.p_total.cumsum() > p).idxmax()

            # densities
            temp = D.filter(regex='^p_', axis=1)
            ax = axiter.grid(1)
            temp.plot(ax=ax, ylim=(0, density_scale), xlim=(0, large_loss_scale), title='Densities')

            ax = axiter.grid(1)
            temp.plot(ax=ax, logx=True, ylim=(0, density_scale), title='Densities log/linear')

            ax = axiter.grid(1)
            temp.plot(ax=ax, logy=True, xlim=(0, large_loss_scale), title='Densities linear/log')

            ax = axiter.grid(1)
            temp.plot(ax=ax, logx=True, logy=True, title='Densities log/log')

            # graph of cumulative loss cost and rate of change of cumulative loss cost
            temp = D.filter(regex='^exa_[^η]')
            # need to check exa actually computed
            if temp.shape[1] == 0:
                print('Update exa before audit plot')
                return

            ax = axiter.grid(1)
            temp.plot(legend=True, ax=ax, xlim=(0, large_loss_scale), ylim=(0, expected_loss_scale),
                      title='Loss Cost by Line: $E(X_i(a))$')

            ax = axiter.grid(1)
            temp.diff().plot(legend=True, ax=ax, xlim=(0, large_loss_scale), ylim=(0, D.index[1]),
                             title='Change in Loss Cost by Line: $\\nabla E(X_i(a))$')

            # E(X_i / X | X > a); exi_x_lea_ dropped
            prefix_and_titles = dict(exi_xgta_=r'$E(X_i/X \mid X>a)$',
                                     exeqa_=r'$E(X_i \mid X=a)$',
                                     exlea_=r'$E(X_i \mid X \leq a)$',
                                     exgta_=r'$E(X_i \mid X>a)$')
            for prefix in prefix_and_titles.keys():
                regex = f'^{prefix}[a-zA-Z]'
                ax = axiter.grid(1)
                D.filter(regex=regex).plot(ax=ax, xlim=(0, large_loss_scale))
                if prefix == 'exgta_':
                    ax.set_title(r'$E(X_i \mid X > a)$ by line and total')
                if prefix.find('xi_x') > 0:
                    # these are fractions between zero and 1; plot sum on same axis and adjust limit
                    D.filter(regex=regex).sum(axis=1).plot(ax=ax, label='calced total')
                    ax.set_ylim(-.05, 1.05)  # so you can see if anything weird is going on
                elif prefix == 'exgta_' or prefix == 'exeqa_':
                    # scale same as x axis - so you can see E(X | X=a) is the diagonal ds
                    ax.set_ylim(0, large_loss_scale)
                else:
                    # scale like mean
                    ax.set_ylim(0, expected_loss_scale)
                ax.set_title(prefix_and_titles[prefix])
                ax.legend(frameon=False)

            # Lee diagrams by peril - will fit in the sixth small plot
            ax = next(axiter)
            # force total first
            ax.plot(D.loc[:, 'p_total'].cumsum(), D.loss, label='total')
            for c in D.filter(regex='^p_[^t]').columns:
                ax.plot(D.loc[:, c].cumsum(), D.loss, label=c[2:])
            ax.legend(frameon=False)
            ax.set_title('Lee Diagram')
            ax.set_xlim(0, 1)
            ax.set_ylim(0, large_loss_scale)

        elif kind == 'priority':
            xmax = self.q(p)
            n_lines = len(self.line_names_ex)
            n_plots = 3 + 2 * n_lines
            if axiter is None:
                axiter = axiter_factory(axiter, n_plots, figsize, height, aspect)

            for prefix, fmt in dict(lev_='LEV', exa_=r'$E(X_i\mid X=a)$', e2pri_=r'$E_2(X_i \mid X=a)$').items():
                ax = axiter.grid(1)
                self.density_df.filter(regex=f'{prefix}').plot(ax=ax, xlim=(0, xmax),
                                                               title=fmt)
                ax.set_xlabel('Capital assets')

            for line in self.line_names:
                ax = axiter.grid(1)
                self.density_df.filter(regex=f'(lev|exa|e2pri)_{line}$').plot(ax=ax, xlim=(0, xmax),
                                                                              title=f'{line.title()} by Priority')
                ax.set_xlabel('Capital assets')
            for col in self.line_names_ex:
                ax = axiter.grid(1)
                self.density_df.filter(regex=f'epd_[012]_{col}').plot(ax=ax, xlim=(0, xmax),
                                                                      title=f'{col.title()} EPDs', logy=True)

        elif kind == 'collateral':
            assert line != '' and line != 'all'
            if axiter is None:
                axiter = axiter_factory(axiter, 2, figsize, height, aspect)

            cmap = cm.BuGn
            if a == 0:
                a = self.q(p)
            pline = self.density_df.loc[0:a, f'p_{line}'].values
            notline = self.density_df.loc[0:a, f'ημ_{line}'].values
            xs = self.density_df.loc[0:a, 'loss'].values
            N = pline.shape[0]
            biv = np.matmul(notline.reshape((N, 1)), pline.reshape((1, N)))
            biv = biv  # / np.sum(np.sum(biv))
            for rho in [1, 0.05]:
                ax = next(axiter)
                ax.imshow(biv ** rho, cmap=cmap, origin='lower', extent=[0, xs[-1], 0, xs[-1]],
                          interpolation='nearest', **kwargs)
                cy = a - c
                ax.plot((c, c), (a - c, xs[-1]), 'k', linewidth=0.5)
                ax.plot((0, a), (a, 0), 'k', linewidth=0.5)
                if c > 0:
                    ax.plot((c, xs[-1]), (cy, xs[-1] * (a / c - 1)), 'k', linewidth=0.5)
                ax.plot((0, c, c), (a - c, a - c, 0), c='k', ls='--', linewidth=0.25)
                ax.set_xlim(0, xs[-1])
                ax.set_ylim(0, xs[-1])
                ax.set_xlabel(f'Line {line}')
                ax.set_ylabel(f'Not {line}')

        else:
            logger.error(f'Portfolio.plot | Unknown plot type {kind}')
            raise ValueError(f'Portfolio.plot unknown plot type {kind}')

        if do_tight:
            axiter.tidy()
            suptitle_and_tight(f'{kind.title()} Plots for {self.name.title()}')

    def uat_interpolation_functions(self, a0, e0):
        """
        Perform quick audit of interpolation functions

        :param a0: base assets
        :param e0: base epd
        :return:
        """
        # audit interpolation functions
        temp = pd.DataFrame(columns=['line', 'priority', 'epd', 'a from e', 'assets', 'e from a'])
        e2a = self.epd_2_assets
        a2e = self.assets_2_epd
        for i in range(3):
            for c in self.line_names + ['total'] + ['not ' + i for i in self.line_names]:
                # if i == 0 and c == 'total' or c != 'total':
                if (c, i) in a2e:
                    e = a2e[(c, i)](a0)
                    a = e2a[(c, i)](e0)
                    temp.loc[c + "_" + str(i), :] = (c, i, e, e2a[(c, i)](e), a, a2e[(c, i)](a))
        display(temp.style)

    def _add_exa(self):
        """
        Use fft to add exa_XXX = E(X_i | X=a) to each dist

        also add exlea = E(X_i | X <= a) = sum_{x<=a} exa(x)*f(x) where f is for the total
        ie. self.density_df['exlea_attrit'] = np.cumsum( self.density_df.exa_attrit *
        self.density_df.p_total) / self.density_df.F

        and add exgta = E(X_i | X>a) since E(X) = E(X | X<= a)F(a) + E(X | X>a)S(a) we have
        exgta = (ex - exlea F) / S

        and add the actual expected losses (not theoretical) the empirical amount:
        self.density_df['e_attrit'] =  np.sum( self.density_df.p_attrit * self.density_df.loss)

        Mid point adjustment is handled by the example creation routines
        self.density_df.loss = self.density_df.loss - bs/2

        **YOU CANNOT HAVE A LINE with a name starting t!!!**

        See LCA_Examples for original code

        Alternative approach to exa: use UC=unconditional versions of exlea and exi_xgta:

        * exleaUC = np.cumsum(port.density_df['exeqa_' + col] * port.density_df.p_total)  # unconditional
        * exixgtaUC =np.cumsum(  self.density_df.loc[::-1, 'exeqa_' + col] / self.density_df.loc[::-1, 'loss']
          * self.density_df.loc[::-1, 'p_total'] )
        * exa = exleaUC + exixgtaUC * self.density_df.loss


        """

        # will need two decorators for epd functions: these handle swapping the arguments and
        # protecting against value errors
        def minus_arg_wrapper(a_func):
            def new_fun(x):
                try:
                    x = a_func(-x)
                except ValueError:
                    x = 999
                return x

            return new_fun

        def minus_ans_wrapper(a_func):
            def new_fun(x):
                try:
                    x = -a_func(x)
                except ValueError:
                    x = 999
                return x

            return new_fun

        # eps is used NOT to do silly things when x is so small F(x)< eps
        # below this percentile you do not try to condition on the event!
        # np.finfo(np.float).eps = 2.2204460492503131e-16
        cut_eps = np.finfo(np.float).eps

        # get this done
        # defuzz(self.density_df, cut_eps)

        # bucket size
        bs = self.bs  # self.density_df.loc[:, 'loss'].iloc[1] - self.density_df.loc[:, 'loss'].iloc[0]
        # index has already been reset

        # sum of p_total is so important...we will rescale it...
        if not np.all(self.density_df.p_total >= 0):
            # have negative densities...get rid of them
            first_neg = np.argwhere(self.density_df.p_total < 0).min()
            logger.warning(
                f'CPortfolio._add_exa | p_total has a negative value starting at {first_neg}; NOT setting to zero...')
            # TODO what does this all mean?!
            # self.density_df.p_total.iloc[first_neg:] = 0
        sum_p_total = self.density_df.p_total.sum()
        logger.info(f'CPortfolio._add_exa | {self.name}: sum of p_total is 1 - '
                    f'{1 - sum_p_total:12.8e} NOT RESCALING')
        # self.density_df.p_total /= sum_p_total
        self.density_df['F'] = np.cumsum(self.density_df.p_total)
        self.density_df['S'] = 1 - self.density_df.F
        # get rounding errors, S may not go below zero
        logger.info(
            f'CPortfolio._add_exa | {self.name}: S <= 0 values has length {len(np.argwhere(self.density_df.S <= 0))}')

        # E(min(X, a))
        # needs to be shifted down by one for the partial integrals....
        # temp = np.hstack((0, np.array(self.density_df.iloc[:-1, :].loc[:, 'S'].cumsum())))
        # self.density_df['exa_total'] = temp * bs
        self.density_df['exa_total'] = self.cumintegral(self.density_df['S'])
        self.density_df.loc[:, 'lev_total'] = self.density_df['exa_total']

        # $E(X\wedge a)=\int_0^a tf(t)dt + aS(a)$ therefore exlea
        # (EXpected $X$ given Less than or Equal to **a**)
        # $$=E(X \mid X\le a)=\frac{E(X\wedge a)-aS(a)}{F(a)}$$
        self.density_df['exlea_total'] = \
            (self.density_df.exa_total - self.density_df.loss * self.density_df.S) / self.density_df.F
        # fix very small values
        # don't pretend you know values!
        # find the largest value where exlea_total > loss, which has to be an error
        # 100 bs is a hack, move a little beyond last problem observation
        # from observation looks about right with 1<<16 buckets
        n_ = self.density_df.shape[0]
        if n_ < 1100:
            mult = 1
        elif n_ < 15000:
            mult = 10
        else:
            mult = 100
        loss_max = self.density_df[['loss', 'exlea_total']].query(' exlea_total>loss ').loss.max()
        if np.isnan(loss_max):
            loss_max = 0
        else:
            loss_max += mult * bs
        self.density_df.loc[0:loss_max, 'exlea_total'] = 0
        # self.density_df.loc[self.density_df.F < 2 * cut_eps, 'exlea_total'] = self.density_df.loc[
        #     self.density_df.F < 2*cut_eps, 'loss']

        # if F(x)<very small then E(X | X<x) = x, you are certain to be above the threshold
        # this is more stable than dividing by the very small F(x)
        self.density_df['e_total'] = np.sum(self.density_df.p_total * self.density_df.loss)
        # epds for total on a stand alone basis (all that makes sense)
        self.density_df.loc[:, 'epd_0_total'] = \
            np.maximum(0, (self.density_df.loc[:, 'e_total'] - self.density_df.loc[:, 'lev_total'])) / \
            self.density_df.loc[:, 'e_total']
        self.density_df['exgta_total'] = self.density_df.loss + (
                self.density_df.e_total - self.density_df.exa_total) / self.density_df.S
        self.density_df['exeqa_total'] = self.density_df.loss  # E(X | X=a) = a(!) included for symmetry was exa

        # E[1/X 1_{X>a}] used for reimbursement effectiveness graph
        index_inv = 1.0 / np.array(self.density_df.index)
        self.density_df['e1xi_1gta_total'] = (self.density_df['p_total'] * index_inv).iloc[::-1].cumsum()

        # FFT functions for use in exa calculations
        # computing sums so minimal padding required
        def loc_ft(x):
            return ft(x, 1, None)

        def loc_ift(x):
            return ift(x, 1, None)

        for col in self.line_names:
            # ### Additional Variables
            #
            # * exeqa_line = $E(X_i \mid X=a)$
            # * exlea_line = $E(X_i \mid X\le a)$
            # * e_line = $E(X_i)$
            # * exgta_line = $E(X_i \mid X \ge a)$
            # * exi_x_line = $E(X_i / X \mid X = a)$
            # * and similar for le and gt a
            # * exa_line = $E(X_i(a))$
            # * Price based on same constant ROE formula (later we will do $g$s)

            # EX_i | X=a, E(xi eq a)
            self.density_df['exeqa_' + col] = \
                np.real(loc_ift(loc_ft(self.density_df.loss * self.density_df['p_' + col]) *
                                loc_ft(self.density_df['ημ_' + col]))) / self.density_df.p_total
            # these are unreliable estimates because p_total=0 JUNE 25: this makes a difference!
            self.density_df.loc[self.density_df.p_total < cut_eps, 'exeqa_' + col] = 0
            self.density_df['exeqa_ημ_' + col] = \
                np.real(loc_ift(loc_ft(self.density_df.loss * self.density_df['ημ_' + col]) *
                                loc_ft(self.density_df['p_' + col]))) / self.density_df.p_total
            # these are unreliable estimates because p_total=0 JUNE 25: this makes a difference!
            self.density_df.loc[self.density_df.p_total < cut_eps, 'exeqa_ημ_' + col] = 0
            # E(X_{i, 2nd priority}(a))
            # need the stand alone LEV calc
            # E(min(Xi, a)
            # needs to be shifted down by one for the partial integrals....
            stemp = 1 - self.density_df.loc[:, 'p_' + col].cumsum()
            # temp = np.hstack((0, stemp.iloc[:-1].cumsum()))
            # self.density_df['lev_' + col] = temp * bs
            self.density_df['lev_' + col] = self.cumintegral(stemp)

            self.density_df['e2pri_' + col] = \
                np.real(loc_ift(loc_ft(self.density_df['lev_' + col]) * loc_ft(self.density_df['ημ_' + col])))
            stemp = 1 - self.density_df.loc[:, 'ημ_' + col].cumsum()
            # temp = np.hstack((0, stemp.iloc[:-1].cumsum()))
            # self.density_df['lev_ημ_' + col] = temp * bs
            self.density_df['lev_ημ_' + col] = self.cumintegral(stemp)

            # EX_i | X<= a; temp is used in le and gt calcs
            temp = np.cumsum(self.density_df['exeqa_' + col] * self.density_df.p_total)
            self.density_df['exlea_' + col] = temp / self.density_df.F
            # revised version for small losses: do not know this value
            self.density_df.loc[0:loss_max, 'exlea_' + col] = 0  # self.density_df.loc[0:loss_max, 'loss']
            temp_not = np.cumsum(self.density_df['exeqa_ημ_' + col] * self.density_df.p_total)
            self.density_df['exlea_ημ_' + col] = temp_not / self.density_df.F
            # revised version for small losses: do not know this value
            self.density_df.loc[0:loss_max, 'exlea_ημ_' + col] = 0  # self.density_df.loc[0:loss_max, 'loss']

            # constant value, helpful in calculations
            self.density_df['e_' + col] = np.sum(self.density_df['p_' + col] * self.density_df.loss)
            self.density_df['e_ημ_' + col] = np.sum(self.density_df['ημ_' + col] * self.density_df.loss)

            # EX_i | X>a
            self.density_df['exgta_' + col] = (self.density_df['e_' + col] - temp) / self.density_df.S

            # E{X_i / X | X > a}  (note=a is trivial!)
            temp = self.density_df.loss.iloc[0]  # loss
            self.density_df.loss.iloc[0] = 1  # avoid divide by zero
            # unconditional E(X_i/X)
            self.density_df['exi_x_' + col] = np.sum(
                self.density_df['exeqa_' + col] * self.density_df.p_total / self.density_df.loss)
            ## DEC 2019 this is a forward sum so it should be cumintegral
            # original
            temp_xi_x = np.cumsum(self.density_df['exeqa_' + col] * self.density_df.p_total / self.density_df.loss)
            # change
            # temp_xi_x = self.cumintegral(self.density_df['exeqa_' + col] * self.density_df.p_total / self.density_df.loss, 1)
            self.density_df['exi_xlea_' + col] = temp_xi_x / self.density_df.F
            self.density_df.loc[0, 'exi_xlea_' + col] = 0  # self.density_df.F=0 at zero
            # more generally F=0 error:
            self.density_df.loc[self.density_df.exlea_total == 0, 'exi_xlea_' + col] = 0
            # not version
            self.density_df['exi_x_ημ_' + col] = np.sum(
                self.density_df['exeqa_ημ_' + col] * self.density_df.p_total / self.density_df.loss)
            # as above
            temp_xi_x_not = np.cumsum(
                self.density_df['exeqa_ημ_' + col] * self.density_df.p_total / self.density_df.loss)
            # temp_xi_x_not = self.cumintegral(
            #     self.density_df['exeqa_ημ_' + col] * self.density_df.p_total / self.density_df.loss, 1)
            self.density_df['exi_xlea_ημ_' + col] = temp_xi_x_not / self.density_df.F
            self.density_df.loc[0, 'exi_xlea_ημ_' + col] = 0  # self.density_df.F=0 at zero
            # more generally F=0 error:
            self.density_df.loc[self.density_df.exlea_total == 0, 'exi_xlea_ημ_' + col] = 0
            # put value back
            self.density_df.loss.iloc[0] = temp
            self.density_df['exi_xgta_' + col] = (self.density_df['exi_x_' + col] - temp_xi_x) / self.density_df.S
            self.density_df['exi_xgta_ημ_' + col] = \
                (self.density_df['exi_x_ημ_' + col] - temp_xi_x_not) / self.density_df.S
            self.density_df['exi_xeqa_' + col] = self.density_df['exeqa_' + col] / self.density_df['loss']
            self.density_df.loc[0, 'exi_xeqa_' + col] = 0
            self.density_df['exi_xeqa_ημ_' + col] = self.density_df['exeqa_ημ_' + col] / self.density_df['loss']
            self.density_df.loc[0, 'exi_xeqa_ημ_' + col] = 0
            # need the loss cost with equal priority rule
            # exa_ = E(X_i(a)) = E(X_i | X<= a)F(a) + E(X_i / X| X>a) a S(a)
            #   = exlea F(a) + exixgta * a * S(a)
            # and hence get loss cost for line i
            self.density_df['exa_' + col] = \
                self.density_df['exlea_' + col] * self.density_df.F + self.density_df.loss * \
                self.density_df.S * self.density_df['exi_xgta_' + col]
            self.density_df['exa_ημ_' + col] = \
                self.density_df['exlea_ημ_' + col] * self.density_df.F + self.density_df.loss * \
                self.density_df.S * self.density_df['exi_xgta_ημ_' + col]

            # E[1/X 1_{X>a}] used for reimbursement effectiveness graph
            self.density_df[f'e1xi_1gta_{col}'] = (self.density_df[f'p_{col}'] * index_inv).iloc[::-1].cumsum()

            # epds
            self.density_df.loc[:, 'epd_0_' + col] = \
                np.maximum(0, (self.density_df.loc[:, 'e_' + col] - self.density_df.loc[:, 'lev_' + col])) / \
                self.density_df.loc[:, 'e_' + col]
            self.density_df.loc[:, 'epd_0_ημ_' + col] = \
                np.maximum(0, (self.density_df.loc[:, 'e_ημ_' + col] - self.density_df.loc[:, 'lev_ημ_' + col])) / \
                self.density_df.loc[:, 'e_ημ_' + col]
            self.density_df.loc[:, 'epd_1_' + col] = \
                np.maximum(0, (self.density_df.loc[:, 'e_' + col] - self.density_df.loc[:, 'exa_' + col])) / \
                self.density_df.loc[:, 'e_' + col]
            self.density_df.loc[:, 'epd_1_ημ_' + col] = \
                np.maximum(0, (self.density_df.loc[:, 'e_ημ_' + col] -
                               self.density_df.loc[:, 'exa_ημ_' + col])) / \
                self.density_df.loc[:, 'e_ημ_' + col]
            self.density_df.loc[:, 'epd_2_' + col] = \
                np.maximum(0, (self.density_df.loc[:, 'e_' + col] - self.density_df.loc[:, 'e2pri_' + col])) / \
                self.density_df.loc[:, 'e_' + col]

            # epd interpolation functions
            # capital and epd functions: for i = 0 and 1 we want line and not line
            loss_values = self.density_df.loss.values
            for i in [0, 1, 2]:
                epd_values = -self.density_df.loc[:, 'epd_{:}_{:}'.format(i, col)].values
                # if np.any(epd_values[1:] <= epd_values[:-1]):
                #     print(i, col)
                #     print( 1e12*(epd_values[1:][epd_values[1:] <= epd_values[:-1]] -
                #       epd_values[:-1][epd_values[1:] <= epd_values[:-1]]))
                # raise ValueError('Need to be sorted ascending')
                self.epd_2_assets[(col, i)] = minus_arg_wrapper(
                    interpolate.interp1d(epd_values, loss_values, kind='linear', assume_sorted=True,
                                         fill_value='extrapolate'))
                self.assets_2_epd[(col, i)] = minus_ans_wrapper(
                    interpolate.interp1d(loss_values, epd_values, kind='linear', assume_sorted=True,
                                         fill_value='extrapolate'))
            for i in [0, 1]:
                epd_values = -self.density_df.loc[:, 'epd_{:}_ημ_{:}'.format(i, col)].values
                self.epd_2_assets[('not ' + col, i)] = minus_arg_wrapper(
                    interpolate.interp1d(epd_values, loss_values, kind='linear', assume_sorted=True,
                                         fill_value='extrapolate'))
                self.assets_2_epd[('not ' + col, i)] = minus_ans_wrapper(
                    interpolate.interp1d(loss_values, epd_values, kind='linear', assume_sorted=True,
                                         fill_value='extrapolate'))

        # put in totals for the ratios... this is very handy in later use
        for metric in ['exi_xlea_', 'exi_xgta_', 'exi_xeqa_']:
            self.density_df[metric + 'sum'] = self.density_df.filter(regex=metric + '[^η]').sum(axis=1)

        epd_values = -self.density_df.loc[:, 'epd_0_total'].values
        # if np.any(epd_values[1:] <= epd_values[:-1]):
        #     print('total')
        #     print(epd_values[1:][epd_values[1:] <= epd_values[:-1]])
        # raise ValueError('Need to be sorted ascending')
        loss_values = self.density_df.loss.values
        self.epd_2_assets[('total', 0)] = minus_arg_wrapper(
            interpolate.interp1d(epd_values, loss_values, kind='linear', assume_sorted=True, fill_value='extrapolate'))
        self.assets_2_epd[('total', 0)] = minus_ans_wrapper(
            interpolate.interp1d(loss_values, epd_values, kind='linear', assume_sorted=True, fill_value='extrapolate'))

    def calibrate_distortion(self, name, r0=0.0, df=5.5, premium_target=0.0, roe=0.0, assets=0.0, p=0.0, S_column='S'):
        """
        Find transform to hit a premium target given assets of ``assets``.
        Fills in the values in ``g_spec`` and returns params and diagnostics...so
        you can use it either way...more convenient


        :param name: name of distortion
        :param r0:   fixed parameter if applicable
        :param premium_target: target premium
        :param roe:             or ROE
        :param assets: asset level
        :param p:
        :param S_column: column of density_df to use for calibration (allows routine to be used in other contexts; if
                so used must input a premium_target directly).
        :return:
        """

        # figure assets
        if S_column == 'S':
            if assets == 0:
                assert (p > 0)
                assets = self.q(p)

            # figure premium target
            if premium_target == 0:
                assert (roe > 0)
                # expected losses with assets
                el = self.density_df.loc[assets, 'exa_total']
                premium_target = (el + roe * assets) / (1 + roe)
        else:
            # no need for roe, set assets = max loss and let code trim it
            assets = self.density_df.loss.iloc[-1]

        # extract S and trim it: we are doing int from zero to assets
        # integration including ENDpoint is
        Splus = self.density_df.loc[0:assets, S_column].values
        last_non_zero = np.argwhere(Splus)
        ess_sup = 0
        if len(last_non_zero) == 0:
            # no nonzero element
            last_non_zero = len(Splus) + 1
        else:
            last_non_zero = last_non_zero.max()
        # remember length = max index + 1 because zero based
        if last_non_zero + 1 < len(Splus):
            # now you have issues...
            # truncate at first zero; numpy indexing because values
            S = Splus[:last_non_zero + 1]
            ess_sup = self.density_df.index[last_non_zero + 1]
            logger.warning(
                'CPortfolio.calibrate_distortion | Mass issues in calibrate_distortion...'
                f'{name} at {last_non_zero}, loss = {ess_sup}')
        else:
            S = self.density_df.loc[0:assets - self.bs, S_column].values

        # now all S values should be greater than zero  and it is decreasing
        assert np.all(S > 0) and np.all(S[:-1] >= S[1:])

        if name == 'ph':
            lS = np.log(S)
            shape = 0.95  # starting param

            def f(rho):
                trho = S ** rho
                ex = np.sum(trho) * self.bs
                ex_prime = np.sum(trho * lS) * self.bs
                return ex - premium_target, ex_prime
        elif name == 'wang':
            n = ss.norm()
            shape = 0.95  # starting param

            def f(lam):
                temp = n.ppf(S) + lam
                tlam = n.cdf(temp)
                ex = np.sum(tlam) * self.bs
                ex_prime = np.sum(n.pdf(temp)) * self.bs
                return ex - premium_target, ex_prime
        elif name == 'ly':
            # linear yield model; min rol is ro/(1+ro)
            shape = 1.25  # starting param
            mass = ess_sup * r0 / (1 + r0)

            def f(rk):
                num = r0 + S * (1 + rk)
                den = 1 + r0 + rk * S
                tlam = num / den
                ex = np.sum(tlam) * self.bs + mass
                ex_prime = np.sum(S * (den ** -1 - num / (den ** 2))) * self.bs
                return ex - premium_target, ex_prime
        elif name == 'clin':
            # capped linear, input rf as min rol
            shape = 1
            mass = ess_sup * r0

            def f(r):
                r0_rS = r0 + r * S
                ex = np.sum(np.minimum(1, r0_rS)) * self.bs + mass
                ex_prime = np.sum(np.where(r0_rS < 1, S, 0)) * self.bs
                return ex - premium_target, ex_prime
        elif name == 'lep':
            # layer equivalent pricing
            # params are d=r0/(1+r0) and delta* = r/(1+r)
            d = r0 / (1 + r0)
            shape = 0.25  # starting param
            # these hard to compute variables do not change with the parameters
            rSF = np.sqrt(S * (1 - S))
            mass = ess_sup * d

            def f(r):
                spread = r / (1 + r) - d
                temp = d + (1 - d) * S + spread * rSF
                ex = np.sum(np.minimum(1, temp)) * self.bs + mass
                ex_prime = (1 + r) ** -2 * np.sum(np.where(temp < 1, rSF, 0)) * self.bs
                return ex - premium_target, ex_prime
        elif name == 'tt':
            # wang-t-t ... issue with df, will set equal to 5.5 per Shaun's paper
            # finding that is a reasonable level for now TODO sort out!
            # param is shape like normal
            t = ss.t(df)
            shape = 0.95  # starting param

            def f(lam):
                temp = t.ppf(S) + lam
                tlam = t.cdf(temp)
                ex = np.sum(tlam) * self.bs
                ex_prime = np.sum(t.pdf(temp)) * self.bs
                return ex - premium_target, ex_prime
        elif name == 'cll':
            # capped loglinear
            shape = 0.95  # starting parameter
            lS = np.log(S)
            lS[0] = 0
            ea = np.exp(r0)

            def f(b):
                uncapped = ea * S ** b
                ex = np.sum(np.minimum(1, uncapped)) * self.bs
                ex_prime = np.sum(np.where(uncapped < 1, uncapped * lS, 0)) * self.bs
                return ex - premium_target, ex_prime
        elif name == 'dual':
            # dual moment
            shape = 2.0  # starting parameter
            S = S[S < 1]
            lS = -np.log(1 - S)  # prob a bunch of zeros...

            # lS[0] = 0  # ??

            def f(rho):
                temp = (1 - S) ** rho
                trho = 1 - temp
                ex = np.sum(trho) * self.bs
                ex_prime = np.sum(temp * lS) * self.bs
                return ex - premium_target, ex_prime
        else:
            raise ValueError(f'calibrate_distortion not implemented for {name}')

        # numerical solve
        i = 0
        fx, fxp = f(shape)
        while abs(fx) > 1e-5 and i < 20:
            shape = shape - fx / fxp
            fx, fxp = f(shape)
            i += 1

        if abs(fx) > 1e-5:
            logger.warning(
                f'CPortfolio.calibrate_distortion | Questionable convergenge! {name}, target '
                f'{premium_target} error {fx}, {i} iterations')

        # build answer (note df is hack for t at the moment TODO)
        dist = Distortion(name=name, shape=shape, r0=r0, df=5.5)
        dist.error = fx
        dist.assets = assets
        dist.premium_target = premium_target
        return dist

    def calibrate_distortions(self, LRs=None, ROEs=None, As=None, Ps=None, r0=0.03, df=5.5, strict=True):
        """
        Calibrate assets a to loss ratios LRs and asset levels As (iterables)
        ro for LY, it :math:`ro/(1+ro)` corresponds to a minimum rate on line


        :param LRs:  LR or ROEs given
        :param ROEs: ROEs override LRs
        :param As:  Assets or probs given
        :param Ps: probability levels for quantiles
        :param r0: for distortions that have a min ROL
        :param df: for tt
        :param strict: if True only use distortions with no mass at zero, otherwise
                        use anything reasonable for pricing
        :return:
        """
        ans = pd.DataFrame(
            columns=['$a$', 'LR', '$S$', '$\\iota$', '$\\delta$', '$\\nu$', '$EL$', '$P$', 'Levg', '$K$',
                     'ROE', 'param', 'error', 'method'], dtype=np.float)
        ans = ans.set_index(['$a$', 'LR', 'method'], drop=True)
        if As is None:
            if Ps is None:
                raise ValueError('Must specify assets or quantile probabilities')
            else:
                As = [self.q(p) for p in Ps]
        for a in As:
            exa, S = self.density_df.loc[a, ['exa_total', 'S']]
            if ROEs is not None:
                # figure loss ratios
                LRs = []
                for r in ROEs:
                    delta = r / (1 + r)
                    nu = 1 - delta
                    prem = nu * exa + delta * a
                    LRs.append(exa / prem)
            for lr in LRs:
                P = exa / lr
                profit = P - exa
                K = a - P
                iota = profit / K
                delta = iota / (1 + iota)
                nu = 1 - delta
                for dname in Distortion.available_distortions(pricing=True, strict=strict):
                    dist = self.calibrate_distortion(name=dname, r0=r0, df=df, premium_target=P, assets=a)
                    ans.loc[(a, lr, dname), :] = [S, iota, delta, nu, exa, P, P / K, K, profit / K,
                                                  dist.shape, dist.error]
        return ans

    def apply_distortions(self, dist_dict, As=None, Ps=None, axiter=None, num_plots=1):
        """
        Apply a list of distortions, summarize pricing and produce graphical output
        show loss values where  :math:`s_ub > S(loss) > s_lb` by jump

        :param dist_dict: dictionary of Distortion objects
        :param As: input asset levels to consider OR
        :param Ps: input probs (near 1) converted to assets using ``self.q()``
        :param num_plots: 0, 1 or 2
        :return:
        """
        ans = []
        if As is None:
            As = np.array([float(self.q(p)) for p in Ps])

        if num_plots == 2 and axiter is None:
            axiter = axiter_factory(None, len(dist_dict))
        elif num_plots == 3 and axiter is None:
            axiter = axiter_factory(None, 30)
        else:
            pass

        for g in dist_dict.values():
            df, au = self.apply_distortion(g, axiter, num_plots)
            # extract range of S values
            temp = df.loc[As, :].filter(regex='^loss|^S|exa[g]?_[^η][\.:~a-zA-Z0-9]*$|exag_sumparts|lr_').copy()
            # jump = sensible_jump(len(temp), num_assets)
            # temp = temp.loc[::jump, :].copy()
            temp['method'] = g.name
            ans.append(temp)

        ans_table = pd.concat(ans)
        ans_table['return'] = np.round(1 / ans_table.S, 0)

        df2 = ans_table.copy()
        df2 = df2.set_index(['loss', 'method', 'return', 'S'])
        df2.columns = df2.columns.str.split('_', expand=True)
        ans_stacked = pd.DataFrame(df2.stack().stack()).reset_index()
        ans_stacked.columns = ['assets', 'method', 'return', 'S', 'line', 'stat', 'value']

        # figure reasonable max and mins for LR plots
        mn = ans_table.filter(regex='^lr').min().min()
        mn1 = mn
        mx = ans_table.filter(regex='^lr').max().max()
        mn = np.round(mn * 20, 0) / 20
        mx = np.round(mx * 20, 0) / 20
        if mx >= 0.9:
            mx = 1
        if mn <= 0.2:
            mn = 0
        if mn1 < mn:
            mn -= 0.1

        # by line columns=method x capital
        if num_plots >= 1:
            sns.catplot(x='line', y='value', row='return', col='method', height=2.5, kind='bar',
                        data=ans_stacked.query(' stat=="lr" ')).set(ylim=(mn, mx), ylabel='LR')
            sns.catplot(x='method', y='value', row='return', col='line', height=2.5, kind='bar',
                        data=ans_stacked.query(' stat=="lr" ')).set(ylim=(mn, mx), ylabel='LR')
            # sns.factorplot(x='return', y='value', row='line', col='method', size=2.5, kind='bar',
            #                data=ans_stacked.query(' stat=="lr" ')).set(ylim=(mn, mx))

        return ans_table, ans_stacked

    def apply_distortion(self, dist, axiter=None, num_plots=0):
        """
        Apply the distortion, make a copy of density_df and append various columns
        Handy graphic of results


        :param dist: CDistortion
        :param axiter: axis iterator, if None no plots are returned
        :param num_plots: =2 plot the sum of parts vs. total plot; =3 go to town
        :return: density_df with extra columns appended
        """
        # store for reference
        self.last_distortion = dist

        # initially work will "full precision"
        # OK to work on original? .copy()  # will be adding columns, do not want to mess up original
        df = self.density_df.copy()

        # make g and ginv and other interpolation functions
        g, g_inv = dist.g, dist.g_inv

        # maybe important that p_total sums to 1
        # this appeared not to make a difference, and introduces an undesirable difference from
        # the original density_df
        # df.loc[df.p_total < 0, :] = 0
        # df['p_total'] = df['p_total'] / df['p_total'].sum()
        # df['F'] = df.p_total.cumsum()
        # df['S'] = 1 - df.F

        # very strangely, THIS HAPPENS, so needs to be corrected...
        df.loc[df.S < 0, 'S'] = 0

        # add the exag and distorted probs
        df['gS'] = g(df.S)
        df['gF'] = 1 - df.gS
        # TODO update for ability to prepend 0 in newer numpy
        df['gp_total'] = np.diff(np.hstack((0, df.gF)))

        # Impact of mass at zero
        # if total has an ess sup < top of computed range then any integral a > ess sup needs to have
        # the mass added. The added mass will be the same for
        mass = 0
        for line in self.line_names:
            # avoid double count: going up sum needs to be stepped one back, hence use cumintegral is perfect
            # for <=a cumintegral,  for > a reverse and use cumsum (no step back)
            # UC = unconditional
            exleaUC = self.cumintegral(self.density_df[f'exeqa_{line}'] * df.gp_total, 1)
            #
            exixgtaUC = np.cumsum(
                self.density_df.loc[::-1, f'exeqa_{line}'] / self.density_df.loc[::-1, 'loss'] *
                df.loc[::-1, 'gp_total'])
            # or shift...NO should be cumsum for gt
            # exixgtaUC1 = self.cumintegral(
            #     self.density_df.loc[::-1, f'exeqa_{line}'] / self.density_df.loc[::-1, 'loss'] *
            #     df.loc[::-1, 'gp_total'], 1)[::-1]
            #
            # if S>0 but flat and there is a mass then need to include loss X g(S(loss)) term!
            # pick  up right hand places where S is very small (rounding issues...)
            if dist.mass:
                mass = dist.mass * self.density_df.loss * self.density_df[f'exi_xeqa_{line}']
            # when computed using np.cumsum exixgtaUC is a pd.Series has an index so when it is mult by .loss
            # (which also has an index) it gets re-sorted into ascending order
            # when computed using cumintegral it is a numpy array with no index and so need reversing
            # the difference between UC and UC1 is a shift up by 1.
            #
            # Here is a little tester example to show what goes on
            #
            # test = pd.DataFrame(dict(x=range(20)))
            # test['a'] = 10 * test.x
            # test['y'] = test.x * 3 + 5
            # bit = np.cumsum(test['y'][::-1])
            # test['z'] = bit
            # test['w'] = bit / test.a
            # test
            #
            df[f'exag_{line}'] = exleaUC + exixgtaUC * self.density_df.loss + mass
            df[f'exleag_{line}'] = exleaUC / df.gF
            df[f'exi_xgtag_{line}'] = exixgtaUC / df.gS
            # these are all here for debugging...see
            # C:\S\TELOS\spectral_risk_measures_monograph\spreadsheets\[AS_IJW_example.xlsx]
            # df[f'exag1_{line}'] = exleaUC + exixgtaUC1 * self.density_df.loss + mass
            # df[f'exi_xgtag1_{line}'] = exixgtaUC1 / df.gS
            # df[f'exleaUC_{line}'] = exleaUC
            # df[f'exleaUCcs_{line}'] = exleaUCcs
            # df[f'U_{line}'] = exixgtaUC
            # df[f'U1_{line}'] = exixgtaUC1
            # df[f'RAW_{line}'] = self.density_df.loc[::-1, f'exeqa_{line}'] / self.density_df.loc[::-1, 'loss'] * \
            #     df.loc[::-1, 'gp_total']
        # sum of parts: careful not to include the total twice!
        df['exag_sumparts'] = df.filter(regex='^exag_[^η]').sum(axis=1)
        # LEV under distortion g
        df['exag_total'] = self.cumintegral(df['gS'])

        # comparison of total and sum of parts
        # removed lTl and pcttotal (which spent a lot of time removing!) Dec 2019
        # Dec 2019 added info to compute the total margin and capital allocation by layer
        # M = marginal margin, T.M = cumulative margin
        # Q same for capital
        #
        # hummmm all these need to be mult by bucket size, no?
        #
        # these are MARGINAL, TOTALs so should really be called M.M_total etc.
        df['M'] = (df.gS - df.S) * self.bs
        df['Q'] = (1 - df.gS) * self.bs
        df['ROE'] = df.M / df.Q
        roe_zero = (df.ROE == 0.0)
        for line in self.line_names_ex:
            df[f'exa_{line}_pcttotal'] = df.loc[:, 'exa_' + line] / df.exa_total
            # exag is the premium
            # df[f'exag_{line}_pcttotal'] = df.loc[:, 'exag_' + line] / df.exag_total
            # premium like Total loss - this is in the aggregate_project and is an exa allocation (obvioulsy)
            # df[f'prem_lTl_{line}'] = df.loc[:, f'exa_{line}_pcttotal'] * df.exag_total
            # df[f'lrlTl_{line}'] = df[f'exa_{line}'] / df[f'prem_lTl_{line}']
            # df.loc[0, f'prem_lTl_{line}'] = 0
            # loss ratio using my allocation
            #
            # TODO check M calcs by doing directly not as diff
            #
            df[f'T.LR_{line}'] = df[f'exa_{line}'] / df[f'exag_{line}']
            df[f'T.M_{line}'] = df[f'exag_{line}'] - df[f'exa_{line}']
            # prepend 1 puts everything off by one
            df[f'M.M_{line}'] = np.diff(df[f'T.M_{line}'], append=0)
            # careful about where ROE==0
            df[f'M.Q_{line}'] = df[f'M.M_{line}'] / df.ROE
            df[f'M.Q_{line}'].iloc[-1] = 0
            df.loc[roe_zero, f'M.Q_{line}'] = np.nan
            # fix ROE indexing error
            # shift up by one
            df[f'T.Q_{line}'] = np.hstack((0, np.cumsum(df[f'M.Q_{line}']).iloc[:-1]))
            df[f'T.ROE_{line}'] = df[f'T.M_{line}'] / df[f'T.Q_{line}']
        # make a convenient audit extract for viewing
        audit = df.filter(regex='^loss|^p_[^η]|^S|^prem|^exag_[^η]|^lr|^z').iloc[0::sensible_jump(len(df), 20), :]

        if num_plots >= 2:
            # short run debugger!
            ax = next(axiter)
            ax.plot(df.exag_sumparts, label='Sum of Parts')
            ax.plot(df.exag_total, label='Total')
            ax.plot(df.exa_total, label='Loss')
            ax.legend()
            ax.set_title(f'Mass audit for {dist.name}')
            ax.legend()

            if num_plots >= 3:
                # yet more graphics, but the previous one is the main event
                # truncate for graphics
                # 1e-4 arb selected min prob for plot truncation... not significant
                max_threshold = 1e-5
                max_x = (df.gS < max_threshold).idxmax()
                max_x = 80000  # TODO>>>
                if max_x == 0:
                    max_x = self.density_df.loss.max()
                df_plot = df.loc[0:max_x, :]
                df_plot = df.loc[0:max_x, :]

                ax = next(axiter)
                df_plot.filter(regex='^p_').sort_index(axis=1).plot(ax=ax)
                ax.set_ylim(0, df_plot.filter(regex='p_[^η]').iloc[1:, :].max().max())
                ax.set_title("Densities")
                ax.legend()

                ax = next(axiter)
                df_plot.loc[:, ['p_total', 'gp_total']].plot(ax=ax)
                ax.set_title("Total Density and Distortion")
                ax.legend()

                ax = next(axiter)
                df_plot.loc[:, ['S', 'gS']].plot(ax=ax)
                ax.set_title("S, gS")
                ax.legend()

                # exi_xlea removed
                for prefix in ['exa', 'exag', 'exlea', 'exeqa', 'exgta', 'exi_xeqa', 'exi_xgta']:
                    # look ahead operator: does not match n just as the next char, vs [^n] matches everything except n
                    ax = next(axiter)  # XXXX??? was (?![n])
                    df_plot.filter(regex=f'^{prefix}_(?!ημ)[a-zA-Z0-9_]+$').sort_index(axis=1).plot(ax=ax)
                    ax.set_title(f'{prefix.title()} by line')
                    ax.legend()
                    if prefix.find('xi_x') > 0:
                        # fix scale for proportions
                        ax.set_ylim(0, 1.05)

                for line in self.line_names:
                    ax = next(axiter)
                    df_plot.filter(regex=f'ex(le|eq|gt)a_{line}').sort_index(axis=1).plot(ax=ax)
                    ax.set_title(f'{line} EXs')
                    ax.legend()

                # compare exa with exag for all lines
                # pno().plot(df_plot.loss, *(df_plot.exa_total, df_plot.exag_total))
                # ax.set_title("exa and exag Total")
                for line in self.line_names_ex:
                    ax = next(axiter)
                    df_plot.filter(regex=f'exa[g]?_{line}$').sort_index(axis=1).plot(ax=ax)
                    ax.set_title(f'{line} EL and Transf EL')
                    ax.legend()

                ax = next(axiter)
                df_plot.filter(regex='^exa_[a-zA-Z0-9_]+_pcttotal').sort_index(axis=1).plot(ax=ax)
                ax.set_title('Pct loss')
                ax.set_ylim(0, 1.05)
                ax.legend()

                ax = next(axiter)
                df_plot.filter(regex='^exag_[a-zA-Z0-9_]+_pcttotal').sort_index(axis=1).plot(ax=ax)
                ax.set_title('Pct premium')
                ax.set_ylim(0, 1.05)
                ax.legend()

                ax = next(axiter)
                df_plot.filter(regex='^M.LR_').sort_index(axis=1).plot(ax=ax)
                ax.set_title('LR: Natural Allocation')
                ax.legend()

                if isinstance(axiter, AxisManager):
                    axiter.tidy()
                # prefer constrained_layout
                # plt.tight_layout()

        return df, audit

    def price(self, reg_g, pricing_g=None):
        """
        Price using regulatory and pricing g functions
            Compute E_price (X wedge E_reg(X) ) where E_price uses the pricing distortion and E_reg uses
            the regulatory distortion

            regulatory capital distortion is applied on unlimited basis: ``reg_g`` can be:

            * if input < 1 it is a number interpreted as a p value and used to deterine VaR capital
            * if input > 1 it is a directly input  capital number
            * d dictionary: Distortion; spec { name = dist name | var | epd, shape=p value a distortion used directly

            ``pricing_g`` is  { name = ph|wang and shape= or lr= or roe= }, if shape and lr or roe shape is overwritten

            if ly it must include ro in spec

            if lr and roe then lr is used

        :param reg_g: a distortion function spec or just a number; if >1 assets if <1 a prob converted to quantile
        :param pricing_g: spec or CDistortion class or lr= or roe =; must have name= to define spec; if CDist that is
                          used
        :return:
        """

        # interpolation functions for distribution and inverse distribution
        F = interpolate.interp1d(self.density_df.loss, self.density_df.F, kind='linear',
                                 assume_sorted=True, bounds_error=False, fill_value='extrapolate')
        Finv = interpolate.interp1d(self.density_df.F, self.density_df.loss, kind='nearest',
                                    assume_sorted=True, fill_value='extrapolate', bounds_error=False)

        # figure regulatory assets; applied to unlimited losses
        a_reg_ix = 0
        a_reg = 0
        if isinstance(reg_g, float) or isinstance(reg_g, int):
            if reg_g > 1:
                a_reg = reg_g
                a_reg_ix = self.density_df.iloc[
                    self.density_df.index.get_loc(reg_g, 'ffill'), 0]
                # print(f'a_reg {a_reg} and ix {a_reg_ix}')
            else:
                a_reg = a_reg_ix = float(Finv(reg_g))
        elif isinstance(reg_g, dict):
            if reg_g['name'] == 'var':  # must be dictionary
                # given var, nearest interpolation for assets
                a_reg = a_reg_ix = float(Finv(reg_g['shape']))
            elif reg_g['name'] == 'epd':
                a_reg = float(self.epd_2_assets[('total', 0)](reg_g['shape']))
                a_reg_ix = self.density_df.iloc[
                    self.density_df.index.get_loc(a_reg, 'ffill'), 0]
            else:
                reg_g = Distortion(**reg_g)
        if a_reg == 0:
            # still need to figure capital
            assert (isinstance(reg_g, Distortion))
            gS = reg_g.g(self.density_df.S)
            a_reg = self.bs * np.sum(gS)
            ix = self.density_df.index.get_loc(a_reg, method='ffill')
            a_reg_ix = self.density_df.index[ix]

        # relevant row for all statistics_df
        row = self.density_df.loc[a_reg_ix, :]

        # figure pricing distortion
        prem = 0
        if isinstance(pricing_g, Distortion):
            # just use it
            pass
        else:
            # spec as dict
            if 'lr' in pricing_g:
                # given LR, figure premium
                prem = row['exa_total'] / pricing_g['lr']
            elif 'roe' in pricing_g:
                # given roe, figure premium
                roe = pricing_g['roe']
                delta = roe / (1 + roe)
                prem = row['exa_total'] + delta * (a_reg - row['exa_total'])
            if prem > 0:
                pricing_g = self.calibrate_distortion(name=pricing_g['name'], premium_target=prem, assets=a_reg_ix)
            else:
                pricing_g = Distortion(**pricing_g)

        # create pricing distortion functions
        g_pri, g_pri_inv = pricing_g.g, pricing_g.g_inv

        # apply pricing distortion to create pricing probs
        # pgS = g_pri(self.density_df.S)
        # pgp_total = -np.diff(np.hstack((0, pgS)))  # adjusted incremental probabilities

        # holder for the answer
        df = pd.DataFrame(columns=['line', 'a_reg', 'exa', 'exag'], dtype=float)
        df.columns.name = 'statistic'
        df = df.set_index('line', drop=True)

        # E_Q((X \wedge a)E(X_i/X|X))
        # W = np.minimum(self.density_df.loss, a_reg_ix)
        # loop through lines and add details
        # the Q measure
        # some g's will return numpy (all except ph in fact return numpy arrays)
        gS = pd.Series(g_pri(self.density_df.S), index=self.density_df.index)
        # make this into a pandas series so the indexing works the same (otherwise it is an np object)
        gp_total = -pd.Series(np.diff(np.hstack((1, gS))), index=self.density_df.index)
        mass = 0
        if pricing_g.has_mass:
            mass = pricing_g.mass
            mass *= a_reg_ix
            logger.info(f'CPortfolio.price | {self.name}, Using mass {mass}')
        for line in self.line_names:
            # int E(Xi/X| X ge x)S = int d/da exa = exa DOES NOT WORK because uses ge x, and that is pre-computed
            # using P and not Q
            # AND have issue of weight at zero applying a capacity charge ??
            # up to a_reg
            # remember loc on df includes RHS
            exag1 = np.sum(self.density_df.loc[0:a_reg_ix - self.bs, f'exeqa_{line}'] *
                           gp_total.loc[0:a_reg_ix - self.bs])
            # note: exi_xeqa = exeqa / loss
            exag2 = np.sum(self.density_df.loc[a_reg_ix:, f'exeqa_{line}'] /
                           self.density_df.loss.loc[a_reg_ix:] * gp_total.loc[a_reg_ix:]) * a_reg_ix
            exag = exag1 + exag2
            if mass > 0:
                # need average EX_i_X for large X, which is tough to compute
                lim_xi_x = self.density_df.loc[a_reg_ix, f'exi_xeqa_{line}']
                exag += lim_xi_x * mass
            # exag = np.sum(W * self.density_df[f'exi_xeqa_{line}'] * pgp_total)
            df.loc[line, :] = [a_reg_ix, row[f'exa_{line}'], exag]

        # total
        line = 'total'
        # if the g fun is degenerate you have the problem of capacity charge
        # so int density does not work. have to use int S
        # apply_distortion uses df['exag_total'] = cumintegral(df['gS'], self.bs)
        # which is the same since it shifts forward
        # old
        # exag = np.sum(g_pri(self.density_df.loc[self.bs:a_reg_ix, 'S'])) * self.bs
        # new
        exag = np.sum(g_pri(self.density_df.loc[0:a_reg_ix - self.bs, 'S'])) * self.bs
        assert (np.isclose(exag, np.sum(gS.loc[0:a_reg_ix - self.bs]) * self.bs))
        df.loc[line, :] = [a_reg_ix, row[f'exa_{line}'], exag]

        # df.loc['sum', :] = df.filter(regex='^[^t]', axis=0).sum()
        df['lr'] = df.exa / df.exag
        df['profit'] = df.exag - df.exa
        df.loc['total', 'ROE'] = df.loc['total', 'profit'] / (df.loc['total', 'a_reg'] - df.loc['total', 'exag'])
        df.loc['total', 'prDef'] = 1 - float(F(a_reg))
        df['pct_loss'] = df.exa / df.loc['total', 'exa']
        df['pct_prem'] = df.exag / df.loc['total', 'exag']
        # ARB asset allocation: same leverage is silly
        # df['a_reg'] = df.loc['total', 'a_reg'] * df.pct_prem
        # same ROE?? NO
        # ROE = df.loc['total', 'profit'] / (df.loc['total', 'a_reg'] - df.loc['total', 'exag'])
        # df['a_reg'] = df.profit / ROE + df.exag
        df['lr'] = df.exa / df.exag
        df['levg'] = df.exag / df.a_reg
        df['ROE'] = df.profit / (df.a_reg - df.exag)
        # for line in self.line_names:
        #     ix = self.density_df.index[ self.density_df.index.get_loc(df.loc[line, 'a_reg'], 'ffill') ]
        #     df.loc[line, 'prDef'] =  np.sum(self.density_df.loc[ix:, f'p_{line}'])
        logger.info(f'CPortfolio.price | {self.name} portfolio pricing g {pricing_g}')
        logger.info(f'CPortfolio.price | Capital sufficient to prob {float(F(a_reg)):7.4f}')
        logger.info(f'CPortfolio.price | Capital quantization error {(a_reg - a_reg_ix) / a_reg:7.5f}')
        if prem > 0:
            logger.info(f'CPortfolio.price | Premium calculated as {prem:18,.1f}')
            logger.info(f'CPortfolio.price | Pricing distortion shape calculated as {pricing_g.shape}')

        return df, pricing_g

    def example_factory(self, dname, dshape=None, dr0=.025, ddf=5.5, LR=None, ROE=None,
                        p=None, A=None, index='loss', plot=True):
        """
        Helpful graphic and summary DataFrames from one distortion, loss ratio and p value.
        Starting logic is the similar to calibrate_distortions.

        Can pass in a pre-calibrated distortion in dname

        Must pass LR or ROE to determine profit

        Must pass p or A to determine assets

        Output is an `Answer` class object containing

                Answer(augmented_df=deets, trinity_df=df, distortion=dist, fig1=f1 if plot else None,
                      fig2=f2 if plot else None, pricing=pricing, exhibit=exhibit, roe_compare=exhibit2,
                      audit_df=audit_df)

        Figures show

        :param dname: name of distortion
        :param dshape:  if input use dshape and dr0 to make the distortion
        :param dr0:
        :param ddf:  r0 and df params for distortion
        :param LR: otherwise use loss ratio and p or a loss ratio
        :param ROE:
        :param p: p value to determine capital.
        :param A:
        :param index:  whether to plot against loss or S(x) NOT IMPLEMENTED
        :param plot:
        :return: various dataframes in an Answer class object

        """

        a = self.q(1 - 1e-8)
        a0 = self.q(1e-4)

        # figure assets a_cal (for calibration) from p or A
        if p is None:
            # have A
            assert A is not None
            exa, p = self.density_df.loc[A, ['exa_total', 'F']]
            a_cal = self.q(p)
            if a_cal != A:
                dev_logger.warning(f'a_cal:=q(p)={a_cal} is not equal to A={A} at p={p}')
        else:
            # have p
            a_cal = self.q(p)
            exa, p = self.density_df.loc[a_cal, ['exa_total', 'F']]

        if dshape is None and not isinstance(dname, Distortion):
            # figure distortion from LR or ROE
            if LR is None:
                assert ROE is not None
                delta = ROE / (1 + ROE)
                nu = 1 - delta
                exag = nu * exa + delta * a_cal
                LR = exa / exag
            else:
                exag = exa / LR

            profit = exag - exa
            K = a_cal - exag
            ROE = profit / K
            cd = self.calibrate_distortions(LRs=[LR], As=[a_cal], r0=dr0, df=ddf)
            dd = Distortion.distortions_from_params(cd, (a_cal, LR), plot=False)
            dist = dd[dname]
            deets, _ = self.apply_distortion(dist)
        else:
            # specified distortion, fill in
            if isinstance(dname, Distortion):
                dist = dname
            else:
                dist = Distortion(dname, dshape, dr0, ddf)
            deets, _ = self.apply_distortion(dist)
            exag = deets.loc[a_cal, 'exag_total']
            profit = exag - exa
            K = a_cal - exag
            ROE = profit / K
            LR = exa / exag

        audit_df = pd.DataFrame(dict(stat=[p, LR, ROE, a_cal, K, dist.name, dist.shape]),
                                index=['p', 'LR', "ROE", 'a_cal', 'K', 'dname', 'dshape'])

        # we now have consistent set of LR, ROE, a_cal, p, exa, exag all computed
        g, g_inv = dist.g, dist.g_inv

        S = deets.S
        # remember loss is the loss limit 'a', not an amount of loss
        loss = deets.loss
        lossa = loss[0:a]
        Sa = S[0:a]
        Fa = 1 - Sa
        gSa = g(Sa)

        # top down stats
        premium_td = np.cumsum(gSa[::-1])[::-1] * self.bs
        el_td = np.cumsum(Sa[::-1])[::-1] * self.bs
        # a - lossa is the amount of capital up to lossa, then premium is the amount of premium upto lossa
        capital_td = (a - lossa) - premium_td
        lr_td = el_td / premium_td
        roe_td = (premium_td - el_td) / capital_td
        leverage_td = premium_td / capital_td
        risk_margin_td = premium_td - el_td
        assets_td = capital_td + premium_td

        # bottom up calc
        premium_bu = np.cumsum(gSa) * self.bs
        el_bu = np.cumsum(Sa) * self.bs
        capital_bu = lossa - premium_bu
        lr_bu = el_bu / premium_bu
        roe_bu = (premium_bu - el_bu) / capital_bu
        leverage_bu = premium_bu / capital_bu
        risk_margin_bu = premium_bu - el_bu
        assets_bu = capital_bu + premium_bu

        # marginal - same td or bu obviously
        marg_roe = (gSa - Sa) / (1 - gSa)
        marg_lr = Sa / gSa

        # extract useful columns only
        # https://regex101.com/r/jN1kL6/1
        regex = '^loss|^p_[^η]|^g?(S|F)|exag?_.+?$(?<!(pcttotal|sumparts))|exi_xgtag?_.+?$(?<!(pcttotal|sumparts))'
        # deets = deets.filter(regex=regex).copy()

        # add various loss ratios
        # these are already in deets, added by apply_distortion)
        # for l in self.line_names_ex:
        #    deets[f'LR_{l}']= deets[f'exa_{l}'] / deets[f'exag_{l}']

        # make really interesting elements for graphing and further analysis
        # note duplicated columns: different names emphasize different viewpoints
        df = pd.DataFrame({'F(a)': Fa, 'a': lossa, 'S(a)': Sa, 'g(S(a))': gSa,
                           'Layer Loss': Sa, 'Layer Prem': gSa, 'Layer Margin': gSa - Sa, 'Layer Capital': 1 - gSa,
                           'Premium↓': premium_td, r'Loss↓': el_td,
                           'Capital↓': capital_td, 'Risk Margin↓': risk_margin_td, 'Assets↓': assets_td,
                           'Loss Ratio↓': lr_td, 'ROE↓': roe_td, 'P:S↓': leverage_td,
                           'Premium↑': premium_bu, r'Loss↑': el_bu,
                           'Capital↑': capital_bu, 'Risk Margin↑': risk_margin_bu, 'Assets↑': assets_bu,
                           'Loss Ratio↑': lr_bu, 'ROE↑': roe_bu, 'P:S↑': leverage_bu,
                           'Marginal LR': marg_lr, 'Marginal ROE': marg_roe, })

        # adjust the top down ROE to reflect that you start writing at a_cal and not a
        df['*ROE↓'] = 0
        df.loc[:a_cal, '*ROE↓'] = (
                (df.loc[:a_cal, 'Premium↓'] - df.loc[:a_cal, 'Loss↓'] -
                 (df.loc[a_cal + self.bs, 'Premium↓'] - df.loc[a_cal + self.bs, 'Loss↓'])) /
                (df.loc[:a_cal, 'Capital↓'] - df.loc[a_cal + self.bs, 'Capital↓'])
        )

        if index == 'loss':
            df = df.set_index('a')
            xlim = [0, a]
        else:
            df = df.set_index('S(a)', drop=False)
            xlim = [-0.05, 1.05]

        # make the pricing summary DataFrame and exhibit
        pricing, exhibit, exhibit2, p_t, pv, pt = self.example_factory_exhibits(deets, p, a_cal)
        # other helpful audit values
        audit_df.loc['TVaR@'] = p_t
        audit_df.loc['erVaR'] = pv
        audit_df.loc['erTVaR'] = pt

        if plot:
            def tidy(a, y=True):
                """
                function to tidy up the graphics
                """
                a.legend(frameon=True)  # , loc='upper right')
                a.set(xlabel='Assets')
                n = 4
                # MaxNLocator uses <= n sensible  points
                a.xaxis.set_minor_locator(AutoMinorLocator(n))
                # fixed just sets that point
                a.xaxis.set_major_locator(FixedLocator([a_cal]))
                # MultipleLocator uses multiples of give value
                # a.xaxis.set_minor_locator(MultipleLocator(a_cal / 5))
                # NullFormatter for no tick marks (default for minor)
                # a.xaxis.set_major_formatter(NullFormatter())
                # StrMethodFormatter suitable for .format(), variable must be called x
                a.xaxis.set_minor_formatter(StrMethodFormatter('{x:,.0f}'))
                ff = f'A={a_cal:,.0f}'
                # Fixed formatter: just give the points
                a.xaxis.set_major_formatter(FixedFormatter([ff]))
                # FuncFormatter also helpful
                # a.xaxis.set_minor_formatter(FuncFormatter(lambda x, pos: f'{x:,.0f}' if x != a_cal else f'**{x:,.0f}'))

                if y:
                    n = 6
                    a.yaxis.set_major_locator(MaxNLocator(n))
                    a.yaxis.set_minor_locator(AutoMinorLocator(4))

                # gridlines with various options
                # https://matplotlib.org/3.1.0/gallery/color/named_colors.html
                a.grid(which='major', axis='x', c='cornflowerblue', alpha=1, linewidth=1)
                a.grid(which='major', axis='y', c='lightgrey', alpha=0.5, linewidth=1)
                a.grid(which='minor', axis='x', c='lightgrey', alpha=0.5, linewidth=1)
                a.grid(which='minor', axis='y', c='gainsboro', alpha=0.25, linewidth=0.5)

                # tick marks
                a.tick_params('x', which='major', labelsize=7, length=10, width=0.75, color='cornflowerblue',
                              direction='out')
                a.tick_params('y', which='major', labelsize=7, length=5, width=0.75, color='black', direction='out')
                a.tick_params('both', which='minor', labelsize=7, length=2, width=0.5, color='black', direction='out')

            # plots
            # https://matplotlib.org/3.1.1/tutorials/intermediate/constrainedlayout_guide.html?highlight=constrained%20layout
            f1, axs = plt.subplots(3, 2, figsize=(8, 10), sharex=True, constrained_layout=True)
            ax = iter(axs.flatten())

            a = next(ax)
            # df[['S(a)', 'g(S(a))', 'F(a)']].plot(xlim=xlim, ax=a).legend(frameon=False, prop={'size': 4}, loc='upper right')
            df[['Layer Loss', 'Layer Prem', 'Layer Capital']].plot(xlim=xlim, logy=False, ax=a)
            tidy(a)
            a.set(ylim=[-0.05, 1.05])

            a = next(ax)
            df[['Layer Capital', 'Layer Margin']].plot(xlim=xlim, ax=a)  # logy=True, ylim=[1e-6,1], ax=a)
            # df[['Layer Loss', 'Layer Prem', 'F(a)', 'Layer Capital', 'Layer Margin']].plot(xlim=xlim, logy=False, ax=a).legend(frameon=False, prop={'size': 6}, loc='upper right')
            tidy(a)
            a.set(ylim=[-0.05, 1.05])

            a = next(ax)
            df[['Premium↓', 'Loss↓', 'Capital↓', 'Assets↓', 'Risk Margin↓']].plot(xlim=xlim, ax=a)
            tidy(a)
            # a.set(aspect=1)

            a = next(ax)
            df[['Loss Ratio↓', 'Loss Ratio↑', 'Marginal LR']].plot(xlim=xlim, ax=a)
            tidy(a)

            a = next(ax)
            df[['Premium↑', 'Loss↑', 'Capital↑', 'Assets↑', 'Risk Margin↑']].plot(xlim=xlim, ax=a)
            tidy(a)
            # a.set(aspect=1)

            a = next(ax)
            # TODO Mess, tidy up
            _ = df.iloc[100:200, :]['Marginal ROE'].max()
            if np.isnan(_):
                _ = 2.5
            ylim = [0, _]
            avg_roe_up = df.at[a_cal, "ROE↑"]
            # just get rid of this
            df.loc[0:self.q(1e-5), 'Marginal ROE'] = np.nan
            df[['ROE↓', '*ROE↓', 'ROE↑', 'Marginal ROE', ]].plot(xlim=xlim, logy=False, ax=a, ylim=ylim)
            # df[['ROE↓', 'ROE↑', 'Marginal ROE', 'P:S↓', 'P:S↑']].plot(xlim=xlim, logy=False, ax=a, ylim=[0,_])
            a.plot(xlim, [avg_roe_up, avg_roe_up], ":", linewidth=2, alpha=0.75, label='Avg ROE')
            tidy(a)
            # a.grid('both','both')

            title = f'{self.name} @ {str(dist)}, LR={LR:.3f} and p={p:.3f}\n' \
                    f'Assets={a_cal:,.1f}, ROE↑={avg_roe_up:.3f}'
            f1.suptitle(title)

            # trinity plots
            def tidy2(a, k, xloc=0.25):

                a.xaxis.set_major_locator(MultipleLocator(xloc))
                a.xaxis.set_minor_locator(AutoMinorLocator(4))
                a.xaxis.set_major_formatter(StrMethodFormatter('{x:.2f}'))

                n = 4
                a.yaxis.set_major_locator(MaxNLocator(2 * n))
                a.yaxis.set_minor_locator(AutoMinorLocator(4))

                # gridlines with various options
                # https://matplotlib.org/3.1.0/gallery/color/named_colors.html
                a.grid(which='major', axis='x', c='lightgrey', alpha=0.5, linewidth=1)
                a.grid(which='major', axis='y', c='lightgrey', alpha=0.5, linewidth=1)
                a.grid(which='minor', axis='x', c='gainsboro', alpha=0.25, linewidth=0.5)
                a.grid(which='minor', axis='y', c='gainsboro', alpha=0.25, linewidth=0.5)

                # tick marks
                a.tick_params('both', which='major', labelsize=7, length=4, width=0.75, color='black', direction='out')
                a.tick_params('both', which='minor', labelsize=7, length=2, width=0.5, color='black', direction='out')

                # line to show where capital lies
                a.plot([0, 1], [k, k], linewidth=1, c='black', label='Capital')

            # https://matplotlib.org/3.1.1/api/_as_gen/matplotlib.gridspec.GridSpec.html#matplotlib.gridspec.GridSpec
            f2, axs = plt.subplots(1, 5, figsize=(8, 3), constrained_layout=True, sharey=True) # this tightens up the grid->, gridspec_kw={'wspace': 0})  # , 'hspace':0

            ax = iter(axs.flatten())

            maxa = self.q(1 - 1e-8)
            k = self.q(p)
            xr = [-0.05, 1.05]

            audit = deets.loc[:maxa, :]

            a = next(ax)
            a.plot(audit.gS, audit.loss, label='Prem')
            a.plot(audit.S, audit.loss, label='Loss')
            a.legend(loc="upper right", frameon=True, edgecolor=None)
            a.set(xlim=xr, title='Prem & Loss')
            a.set(xlabel='Loss = S = Pr(X>a)\nPrem = g(S)', ylabel="Assets, a")
            tidy2(a, k)

            a = next(ax)
            m = audit.F - audit.gF
            a.plot(m, audit.loss, linewidth=2, label='M')
            a.set(xlim=-0.01, title='Margin, M', xlabel='M = g(S) - S')
            tidy2(a, k, m.max() * 1.05 / 4)

            a = next(ax)
            a.plot(1 - audit.gS, audit.loss, label='Q')
            a.set(xlim=xr, title='Equity, Q')
            a.set(xlabel='Q = 1 - g(S)')
            tidy2(a, k)

            a = next(ax)
            temp = audit.loc[self.q(1e-5):, :]
            r = (temp.gS - temp.S) / (1 - temp.gS)
            a.plot(r, temp.loss, linewidth=2, label='ROE')
            a.set(xlim=-0.05, title='Layer ROE')
            a.set(xlabel='ROE = M / Q')
            tidy2(a, k, r.max() * 1.05 / 4)

            a = next(ax)
            a.plot(audit.S / audit.gS, audit.loss)
            a.set(xlim=xr, title='Layer LR')
            a.set(xlabel='LR = S / g(S)')
            tidy2(a, k)

        return Answer(augmented_df=deets, trinity_df=df, distortion=dist, fig1=f1 if plot else None,
                      fig2=f2 if plot else None, pricing=pricing, exhibit=exhibit, roe_compare=exhibit2,
                      audit_df=audit_df)

    def example_factory_exhibits(self, data_in, p=0., a=0.):
        """
        do the work to extract the pricing, exhibit and exhibit 2 DataFrames from deets
        Can also accept an ans object with an augmented_df element (how to call from outside)
        POINT: re-run exhibits at different p/a thresholds without recalibrating
        add relevant items to audit_df
        a = q(p) if both given; if not both given derived as usual
        """
        assert p or a
        if p and not a:
            a = self.q(p)
        if a and not p:
            p = self.cdf(a)
        if isinstance(data_in, Answer):
            deets = data_in.augmented_df
        else:
            deets = data_in

        ex = deets.loc[[a]].T
        ex.index = [i.replace('xi_x', 'xi/x').replace('epd_', 'epd:').
                        replace('ημ_', 'ημ') for i in ex.index]
        pricing = ex.filter(regex='loss|^p_|g?[S]|exi/xgtag?_.+?$(?<!(sum))|'
                                  'exag?_.+?$(?<!(pcttotal|sumparts))|LR_', axis=0).sort_index()
        pricing.index = [i if i.find('_') > 0 else f'{i}_total' for i in pricing.index]
        pricing.index = pricing.index.str.split('_', expand=True)
        pricing.index.set_names(['stat', 'line'], inplace=True)
        pricing = pricing.sort_index(level=[0, 1])
        # !!! TODO WTF Names!!
        pricing.loc[('exi/xgta', 'total'), :] = pricing.loc[('exi/xgta', slice('Atame', 'Dthick')), :].sum(axis=0)
        pricing = pricing.sort_index(level=[0, 1])
        pricing.loc[('exi/xgtag', 'total'), :] = pricing.loc[('exi/xgtag', slice('Atame', 'Dthick')), :].sum(axis=0)
        pricing = pricing.sort_index(level=[0, 1])
        for l in self.line_names:
            pricing.loc[('S', l), :] = \
                pricing.loc[('exi/xgta', l), :].values * pricing.loc[('S', 'total'), :].values
            pricing.loc[('gS', l), :] = \
                pricing.loc[('exi/xgtag', l), :].values * pricing.loc[('gS', 'total'), :].values
            pricing.loc[('loss', l), :] = pricing.loc[('loss', 'total')]
        pricing = pricing.sort_index(level=[0, 1])

        # focus on just the indicated capital level a
        exhibit = pricing.xs(a, axis=1).unstack(1).T.copy()
        exhibit['M.LR'] = exhibit.S / exhibit.gS
        # ROE.2 is the total ROE
        # exhibit['M.ROE.2'] = (exhibit.gS - exhibit.S) / (1 - exhibit.gS)
        # !!!!!!!!!
        exhibit['M.Q'] = ex.filter(regex=r'^M\.Q_', axis=0).values
        exhibit['T.Q'] = ex.filter(regex=r'^T\.Q_', axis=0).values
        exhibit['T.ROE'] = ex.filter(regex=r'^T\.ROE_', axis=0).values
        exhibit['M.ROE'] = float(ex.filter(regex='^ROE', axis=0).values)
        exhibit['T.M'] = ex.filter(regex='^T.M_', axis=0).values

        Q = a - exhibit.at['total', 'exag']
        roe = (exhibit.at['total', 'exag'] - exhibit.at['total', 'exa']) / Q
        # exhibit['T.Q'] = (exhibit.exag -  exhibit.exa) / roe
        # exhibit['M.Q'] = 1 - exhibit.gS
        # this is fine...but we have it in T.M
        # exhibit['T.M.2'] = exhibit.exag - exhibit.exa
        # exhibit['T.ROE'] = roe
        exhibit['A'] = a
        exhibit = exhibit[
            ['A', 'S', 'gS', 'M.LR', 'M.Q', 'M.ROE', 'exi/xgta', 'exi/xgtag', 'exa', 'exag', 'T.M', 'T.LR', 'T.Q',
             'T.ROE']]
        exhibit.columns = ['A', 'M.L', 'M.P', 'M.LR', 'M.Q', 'M.ROE', 'exi/xgta', 'exi/xgtag', 'T.L', 'T.P', 'T.M',
                           'T.LR', 'T.Q', 'T.ROE']
        exhibit['M.M'] = exhibit['M.P'] - exhibit['M.L']
        exhibit['T.PQ'] = exhibit['T.P'] / exhibit['T.Q']

        # some reasonable traditional metrics
        # tvar threshold giving the same assets as p on VaR
        try:
            p_t = self.tvar_threshold(p)
        except ValueError as e:
            dev_logger.warning(f'Error computing p_t threshold for VaR at p={p}')
            logger.warning(str(e))
            p_t = 0.5
        try:
            pv, pt = self.equal_risk_var_tvar(p, p_t)
        except (ZeroDivisionError, ValueError) as e:
            dev_logger.warning(f'Error computing p_t threshold for VaR, p={p}, p_t={p_t}')
            logger.warning(str(e))
            pv = 0.5
            pt = 0.5
        try:
            done = []
            exhibit['VaR'] = [float(a.middle_q(p)) for a in self] + [self.q(p)]
            done.append('var')
            exhibit['TVaR'] = [float(a.tvar(p_t)) for a in self] + [self.tvar(p_t)]
            done.append('tvar')
            exhibit['ScaledVaR'] = exhibit.VaR
            exhibit['ScaledTVaR'] = exhibit.TVaR
            exhibit.loc['total', 'ScaledVaR'] = 0
            exhibit.loc['total', 'ScaledTVaR'] = 0
            sumvar = exhibit.ScaledVaR.sum()
            sumtvar = exhibit.ScaledTVaR.sum()
            exhibit['ScaledVaR'] = exhibit.ScaledVaR * exhibit.loc['total', 'VaR'] / sumvar
            exhibit['ScaledTVaR'] = exhibit.ScaledTVaR * exhibit.loc['total', 'TVaR'] / sumtvar
            exhibit.loc['total', 'ScaledVaR'] = exhibit.loc['total', 'VaR']
            exhibit.loc['total', 'ScaledTVaR'] = exhibit.loc['total', 'TVaR']
            exhibit.loc['total', 'VaR'] = sumvar
            exhibit.loc['total', 'TVaR'] = sumtvar
            exhibit['EqRiskVaR'] = [float(a.middle_q(pv)) for a in self] + [self.q(p)]
            done.append('eqvar')
            exhibit['EqRiskTVaR'] = [float(a.tvar(pt)) for a in self] + [self.tvar(p_t)]
            done.append('eqtvar')
            # MerPer
            exhibit['MerPer'] = self.merton_perold(p)
            done.append('merper')
        except ValueError as e:
            dev_logger.warning('Some general out of bounds error on VaRs and TVaRs, setting all equal to zero.'
                               f'Completed steps out of var, tvar, eqvar, eqtvar merper are {done}')
            logger.warning(str(e))
            for c in ['VaR', 'TVaR', 'ScaledVaR', 'ScaledTVaR', 'EqRiskVaR', 'EqRiskTVaR', 'MerPer']:
                if c not in exhibit.columns:
                    exhibit[c] = 0.0
                else:
                    exhibit[c] = -999
        # EPD
        row = self.density_df.loc[a, :]
        exhibit['EPD'] = [row.at[f'epd_{0 if l == "total" else 1}_{l}'] for l in self.line_names_ex]
        # subtract the premium to get the actual capital
        methods = ['VaR', 'TVaR', 'ScaledVaR', 'ScaledTVaR', 'EqRiskVaR', 'EqRiskTVaR', 'MerPer']
        exhibit.loc[:, methods] = exhibit.loc[:, methods].sub(exhibit['T.P'].values, axis=0)
        # make a version just focused on comparing ROEs
        cols = ['ScaledVaR', 'ScaledTVaR', 'EqRiskVaR', 'EqRiskTVaR', 'MerPer']
        exhibit2 = exhibit.copy()[['T.P', 'T.L', 'T.LR', 'T.Q', 'T.ROE'] + cols]
        exhibit2.loc[:, cols] = (1 / exhibit2[cols]).mul(exhibit2['T.P'] - exhibit2['T.L'], axis=0)

        # return depending on how called
        if isinstance(data_in, Answer):
            return Answer(pricing=pricing, exhibit=exhibit, exhibit2=exhibit2)
            # deets = data_in.augmented_df
        else:
            return pricing, exhibit, exhibit2, p_t, pv, pt

    def top_down(self, distortions, A_or_p):
        """
        DataFrame summary and nice plots showing marginal and average ROE, lr etc. as you write a layer from x to A
        If A=0 A=q(log) is used

        Not integrated into graphics format (plot)

        :param distortions: list or dictionary of CDistortion objects, or a single CDist object
        :param A_or_p: if <1 interpreted as a quantile, otherwise assets
        :return:
        """

        logger.warning('Portfolio.top_down is deprecated. It has been replaced by Portfolio.example_factory.')

        assert A_or_p > 0

        if A_or_p < 1:
            # call with one arg and interpret as log
            A = self.q(A_or_p)
        else:
            A = A_or_p

        if isinstance(distortions, dict):
            list_specs = distortions.values()
        elif isinstance(distortions, list):
            list_specs = distortions
        else:
            list_specs = [distortions]

        dfs = []
        for dist in list_specs:
            g, g_inv = dist.g, dist.g_inv

            S = self.density_df.S
            loss = self.density_df.loss

            a = A - self.bs  # A-bs for pandas series (includes endpoint), a for numpy indexing; int(A / self.bs)
            lossa = loss[0:a]

            Sa = S[0:a]
            Fa = 1 - Sa
            gSa = g(Sa)
            premium = np.cumsum(gSa[::-1])[::-1] * self.bs
            el = np.cumsum(Sa[::-1])[::-1] * self.bs
            capital = A - lossa - premium
            risk_margin = premium - el
            assets = capital + premium
            marg_roe = (gSa - Sa) / (1 - gSa)
            lr = el / premium
            roe = (premium - el) / capital
            leverage = premium / capital
            # rp = -np.log(Sa) # return period
            marg_lr = Sa / gSa

            # sns.set_palette(sns.color_palette("Paired", 4))
            df = pd.DataFrame({'$F(x)$': Fa, '$x$': lossa, 'Premium': premium, r'$EL=E(X\wedge x)$': el,
                               'Capital': capital, 'Risk Margin': risk_margin, 'Assets': assets, '$S(x)$': Sa,
                               '$g(S(x))$': gSa, 'Loss Ratio': lr, 'Marginal LR': marg_lr, 'ROE': roe,
                               'Marginal ROE': marg_roe, 'P:S levg': leverage})
            df = df.set_index('$F(x)$', drop=True)
            df.plot(subplots=True, rot=0, figsize=(14, 4), layout=(-1, 7))
            suptitle_and_tight(f'{str(dist)}: Statistics for Layer $x$ to $a$ vs. $F(x)$')
            df['distortion'] = dist.name
            dfs.append(df)
        return pd.concat(dfs)

    def analysis_priority(self, asset_spec, output='df'):
        """
        Create priority analysis report_ser.
        Can be called multiple times with different ``asset_specs``
        asset_spec either a float used as an epd percentage or a dictionary. Entering an epd percentage
        generates the dictionary

                base = {i: self.epd_2_assets[('not ' + i, 0)](asset_spec) for i in self.line_names}

        :param asset_spec: epd
        :param output: df = pandas data frame; html = nice report, markdown = raw markdown text
        :return:
        """

        ea = self.epd_2_assets
        ae = self.assets_2_epd

        if isinstance(asset_spec, dict):
            base = asset_spec
        else:
            if type(asset_spec) != float:
                raise ValueError("Input dictionary or float = epd target")
            base = {i: ea[('not ' + i, 0)](asset_spec) for i in self.line_names}

        if output == 'df':
            priority_analysis_df = pd.DataFrame(
                columns=['a', 'chg a', 'not_line epd sa @a', 'line epd @a 2pri', 'not_line epd eq pri',
                         'line epd eq pri', 'total epd'],
                index=pd.MultiIndex.from_arrays([[], []], names=['Line', 'Scenario']))
            for col in set(self.line_names).intersection(set(base.keys())):
                notcol = 'not ' + col
                a_base = base[col]
                a = a_base
                e0 = ae[(notcol, 0)](a_base)
                e = e0
                priority_analysis_df.loc[(col, 'base'), :] = (
                    a, a - a_base, e, ae[(col, 2)](a), ae[(notcol, 1)](a), ae[(col, 1)](a), ae[('total', 0)](a))

                a = ea[(col, 2)](e0)
                priority_analysis_df.loc[(col, '2pri line epd = not line sa'), :] = (
                    a, a - a_base, ae[(notcol, 0)](a), ae[(col, 2)](a), ae[(notcol, 1)](a), ae[(col, 1)](a),
                    ae[('total', 0)](a))

                a = ea[(col, 2)](priority_analysis_df.ix[(col, 'base'), 'line epd eq pri'])
                priority_analysis_df.loc[(col, 'thought buying (line 2pri epd = base not line eq pri epd'), :] = (
                    a, a - a_base, ae[(notcol, 0)](a), ae[(col, 2)](a), ae[(notcol, 1)](a), ae[(col, 1)](a),
                    ae[('total', 0)](a))

                a = ea[(notcol, 1)](e0)
                priority_analysis_df.loc[(col, 'fair to not line, not line eq pri epd = base sa epd'), :] = (
                    a, a - a_base, ae[(notcol, 0)](a), ae[(col, 2)](a), ae[(notcol, 1)](a), ae[(col, 1)](a),
                    ae[('total', 0)](a))

                a = ea[(col, 1)](e0)
                priority_analysis_df.loc[(col, 'line eq pri epd = base not line sa'), :] = (
                    a, a - a_base, ae[(notcol, 0)](a), ae[(col, 2)](a), ae[(notcol, 1)](a), ae[(col, 1)](a),
                    ae[('total', 0)](a))

                a = ea[('total', 0)](e0)
                priority_analysis_df.loc[(col, 'total epd = base sa not line epd'), :] = (
                    a, a - a_base, ae[(notcol, 0)](a), ae[(col, 2)](a), ae[(notcol, 1)](a), ae[(col, 1)](a),
                    ae[('total', 0)](a))

            priority_analysis_df.loc[:, 'pct chg'] = priority_analysis_df.loc[:, 'chg a'] / priority_analysis_df.a
            return priority_analysis_df

        # else HTML or markdown output
        ans = []
        for line in set(self.line_names).intersection(set(base.keys())):
            a = base[line]
            e = ae[(f'not {line}', 0)](a)
            a0 = float(ea[('total', 0)](e))
            eb0a0 = ae[(f'not {line}', 0)](a0)
            eba0 = ae[(f'not {line}', 1)](a0)
            e2a0 = ae[(line, 2)](a0)
            e1a0 = ae[(line, 1)](a0)
            e2 = ae[(line, 2)](a)
            e1 = float(ae[(line, 1)](a))
            a2 = float(ea[(line, 2)](e1))
            af = float(ea[(f'not {line}', 1)](e))
            af2 = float(ea[(line, 1)](e))
            a3 = float(ea[(line, 2)](e))
            a4 = float(ea[(f'not {line}', 1)](e))

            story = f"""
Consider adding **{line}** to the existing portfolio. The existing portfolio has capital {a:,.1f} and and epd of {e:.4g}.

* If {line} is added as second priority to the existing lines with no increase in capital it has an epd of {e2:.4g}.
* If the regulator requires the overall epd be a constant then the firm must increase capital to {a0:,.1f} or by {(a0 / a - 1) * 100:.2f} percent.
    - At the higher capital {line} has an epd of {e2a0:.4g} as second priority and the existing lines have an epd of {eb0a0:.4g} as first priority.
    - The existing and {line} epds under equal priority are {eba0:.4g} and {e1a0:.4g}.
* If {line} *thought* it was added at equal priority it would have expected an epd of {e1:.4g}.
  In order to achieve this epd as second priority would require capital of {a2:,.1f}, an increase of {(a2 / a - 1) * 100:.2f} percent.
* In order for {line} to have an epd equal to the existing lines as second priority would require capital
  of {a3:,.1f}, and increase of {(a3 / a - 1) * 100:.2f} percent.
* In order for {line} to be added at equal priority and for the existing lines to have an unchanged epd requires capital of {af:,.1f}, an
  increase of {(af / a - 1) * 100:.2f} percent.
* In order for {line} to be added at equal priority and to have an epd equal to the existing line epd requires capital of {af2:,.1f}, an
  increase of {(af2 / a - 1) * 100:.2f} percent.
* In order for the existing lines to have an unchanged epd at equal priority requires capital of {a4:,.1f}, an increase of {(a4 / a - 1) * 100:.2f} percent.
"""
            ans.append(story)
        ans = '\n'.join(ans)
        if output == 'html':
            display(HTML(pypandoc.convert_text(ans, to='html', format='markdown')))
        else:
            return ans

    def analysis_collateral(self, line, c, a, debug=False):
        """
        E(C(a,c)) expected value of line against not line with collateral c and assets a, c <= a

        :param line: line of business with collateral, analyzed against not line
        :param c: collateral, c <= a required; c=0 reproduces exa, c=a reproduces lev
        :param a: assets, assumed less than the max loss (i.e. within the square)
        :param debug:
        :return:
        """
        assert (c <= a)
        xs = self.density_df.loc[:, 'loss'].values
        pline = self.density_df.loc[:, 'p_' + line].values
        notline = self.density_df.loc[:, 'ημ_' + line].values
        ans = []
        gt, incr, int1, int2, int3 = 0, 0, 0, 0, 0
        c1, c2, c3 = 0, 0, 0
        n_c = int(c / self.bs)
        n_max = len(xs)  # this works as a rhs array[0:n_max] is the whole array, array[n_max] is an error
        err_count = 0
        for loss in np.arange(a + self.bs, 2 * xs.max(), self.bs):
            n_loss = int(loss / self.bs)  # 0...loss INCLUSIVE
            c1 = c / a * loss
            n_c1 = min(n_max, int(c1 / self.bs))
            # need to adjust for trimming when loss > max loss
            # loss indexes...see notes in blue book
            la = max(0, n_loss - (n_max - 1))
            lc = min(n_loss, n_max - 1)
            lb = lc + 1
            if la == 0:
                ld = None
            else:
                ld = la - 1
            try:
                s1 = slice(la, max(la, min(lb, n_c)))
                s2 = slice(max(la, min(lb, n_c)), max(la, min(lb, n_c1)))
                s3 = slice(max(la, min(lb, n_c1)), lb)
                if ld is None:
                    # means go all the way to zero, do not have to worry about n_loss - n_c > 0 being smaller
                    s1r = slice(lc, min(lc, n_loss - n_c), -1)
                    s2r = slice(min(lc, n_loss - n_c), min(lc, n_loss - n_c1), -1)
                    s3r = slice(min(lc, n_loss - n_c1), ld, -1)
                else:
                    s1r = slice(lc, max(ld, min(lc, n_loss - n_c)), -1)
                    s2r = slice(max(ld, min(lc, n_loss - n_c)), max(ld, min(lc, n_loss - n_c1)), -1)
                    s3r = slice(max(ld, min(lc, n_loss - n_c1)), ld, -1)
                int1 = np.sum(xs[s1] * pline[s1] * notline[s1r])
                int2 = c * np.sum(pline[s2] * notline[s2r])
                int3 = a / loss * np.sum(xs[s3] * pline[s3] * notline[s3r])
                ptot = np.sum(pline[s3] * notline[s3r])
            except ValueError as e:
                print(e)
                print(f"Value error: loss={loss}, loss/b={loss / self.bs}, c1={c1}, c1/b={c1 / self.bs}")
                print(f"n_loss {n_loss},  n_c {n_c}, n_c1 {n_c1}")
                print(f'la={la}, lb={lb}, lc={lc}, ld={ld}')
                print('ONE:', *map(len, [xs[s1], pline[s1], notline[s1r]]))
                print('TWO:', *map(len, [pline[s2], notline[s2r]]))
                print('THR:', *map(len, [xs[s3], pline[s3], notline[s3r]]))
                err_count += 1
                if err_count > 3:
                    break
            if n_loss < n_max:
                p = self.density_df.loc[loss, 'p_total']
            else:
                p = np.nan
            incr = (int1 + int2 + int3)
            gt += incr
            c1 += int1
            c2 += int2
            c3 += int3
            if debug:
                ans.append([loss, int1, int2, int3, int3 * loss / a / ptot, ptot, incr, c1, c2, c3, gt, p])
            if incr / gt < 1e-12:
                if debug:
                    logger.info(f'incremental change {incr / gt:12.6f}, breaking')
                break
        exlea = self.density_df.loc[a, 'exlea_' + line]
        exgta = self.density_df.loc[a, 'exgta_' + line]
        exix = self.density_df.loc[a, 'exi_xgta_' + line]
        exeqa = self.density_df.loc[a, 'exeqa_' + line]
        p_total = self.density_df.loc[a, 'p_total']
        F = self.density_df.loc[a, 'F']
        exa = self.density_df.loc[a, 'exa_' + line]
        lev = self.density_df.loc[a, 'lev_' + line]
        df = pd.DataFrame(
            [(line, a, c, p_total, F, gt, a * exix * (1 - F), exeqa, exlea, exgta, exix, exa, gt + exlea * F, lev)],
            columns=['line', 'a', 'c', 'p_total', 'F', 'gt', 'exa_delta', 'exeqa', 'exlea', 'exgta', 'exix', 'exa',
                     'ecac', 'lev'],
        )
        if debug:
            ans = pd.DataFrame(ans,
                               columns=['loss', 'int1', 'int2', 'int3', 'exeqa', 'ptot', 'incr', 'c1', 'c2', 'c3', 'gt',
                                        'log'])
            ans = ans.set_index('loss', drop=True)
            ans.index.name = 'loss'
        else:
            ans = None
        return df, ans

    def uat_differential(self, line):
        """
        Check the numerical and theoretical derivatives of exa agree for given line

        :param line:
        :return:
        """

        test = self.density_df[f'exa_{line}']
        dtest = np.gradient(test, test.index)
        dtest2 = self.density_df.loc[:, f'exi_xgta_{line}'] * self.density_df.S

        ddtest = np.gradient(dtest)
        ddtest2 = -self.density_df.loc[:, f'exeqa_{line}'] / self.density_df.loss * self.density_df.p_total

        f, axs = plt.subplots(1, 3, figsize=(12, 4))
        axs[0].plot(test.index, test, label=f'exa_{line}')

        axs[1].plot(test.index, dtest, label='numdiff')
        axs[1].plot(test.index, dtest2, label='xi_xgta S(x)')
        axs[1].legend()

        axs[2].plot(test.index, ddtest, label='numdiff')
        axs[2].plot(test.index, ddtest2, label='-EXi(a)/a')
        axs[2].legend()

    def uat(self, As=None, Ps=[0.98], LRs=[0.965], r0=0.03, num_plots=1, verbose=False):
        """
        Reconcile apply_distortion(s) with price and calibrate


        :param As:   Asset levels
        :param Ps:   probability levels used to determine asset levels using quantile function
        :param LRs:  loss ratios used to determine profitability
        :param r0:   r0 level for distortions
        :param verbose: controls level of output
        :return:
        """

        # figure As
        if As is None:
            As = []
            for p in Ps:
                As.append(self.q(p))

        # 0. Calibrate
        params = self.calibrate_distortions(LRs=LRs, As=As, r0=r0)

        # 1. Apply and compare to calibration
        K = As[0]
        LR = LRs[0]
        idx = (K, LR)
        dd = Distortion.distortions_from_params(params, index=idx, r0=r0, plot=False)
        if num_plots == 2:
            axiter = axiter_factory(None, len(dd))
        elif num_plots == 3:
            axiter = axiter_factory(None, 30)
        else:
            axiter = None
        table, stacked = self.apply_distortions(dd, As, axiter, num_plots)
        table['lr err'] = table['lr_total'] - LR

        # 2. Price and compare to calibration
        pdfs = []  # pricing data frmes
        for name in Distortion.available_distortions():
            pdf, _ = self.price(reg_g=K, pricing_g=dd[name])
            pdf['dist'] = name
            pdfs.append(pdf)
        p = pd.concat(pdfs)
        p['lr err'] = p['lr'] - LR

        # a from apply, p from price
        a = table.query(f' loss=={K} ')

        # easier tests
        # sum of parts = total
        logger.info(
            f'Portfolio.uat | {self.name} Sum of parts all close to total: '
            f'{np.allclose(a.exag_total, a.exag_sumparts)}')
        logger.info(
            f'Portfolio.uat | {self.name} Sum of parts vs total: '
            f'{np.sum(np.abs(a.exag_total - a.exag_sumparts)):15,.1f}')

        pp = p[['dist', 'exag']]
        pp = pp.pivot(columns='dist').T.loc['exag']
        aa = a.filter(regex='exa|method').set_index('method')

        test = pd.concat((aa, pp), axis=1, sort=True)
        for c in self.line_names_ex:
            test[f'err_{c}'] = test[c] / test[f'exag_{c}'] - 1
        test['err sum/total'] = test['exag_sumparts'] / test['exag_total'] - 1
        test = test[
            [f'{i}{j}' for j in self.line_names_ex for i in ['exag_', '', 'err_']] + ['exag_sumparts', 'err sum/total']]
        lr_err = pd.DataFrame({'applyLR': a.lr_total, 'method': a.method, 'target': LR, 'errs': a.lr_total - LR})
        lr_err = lr_err.reset_index(drop=False).set_index('method')
        lr_err = lr_err.rename(columns={'index': 'a'})
        test = pd.concat((test, lr_err), axis=1, sort=True)
        overall_test = (test.filter(regex='err').abs()).sum().sum()
        if verbose:
            html_title(f'Combined, overall error {overall_test:.3e}')  # (exag=apply)')
            display(test)

        if lr_err.errs.abs().max() > 1e-4:
            logger.error('Portfolio.uat | {self.name} UAT Loss Ratio Error {lr_err.errs.abs().max()}')

        if overall_test < 1e-7:
            logger.info(f'Portfolio.uat | {self.name} UAT All good, total error {overall_test:6.4e}')
        else:
            s = f'{self.name} UAT total error {overall_test:6.4e}'
            logger.error(f'Portfolio.uat | {s}')
            logger.error(f'Portfolio.uat | {s}')
            logger.error(f'Portfolio.uat | {s}')

        return a, p, test, params, dd, table, stacked

    def cumintegral(self, v, bs_override=0):
        """
        cumulative integral of v with buckets size bs

        :param bs_override:
        :param v:
        :return:
        """

        if bs_override != 0:
            bs = bs_override
        else:
            bs = self.bs

        if type(v) == np.ndarray:
            return np.hstack((0, v[:-1])).cumsum() * bs
        else:
            return np.hstack((0, v.values[:-1])).cumsum() * bs

    @staticmethod
    def from_DataFrame(name, df):
        """
        create portfolio from pandas dataframe
        uses columns with appropriate names

        Can be fed the agg output of uw.write_test( agg_program )

        :param name:
        :param df:
        :return:
        """
        # ...and this is why we love pandas so much
        spec_list = [g.dropna(axis=1).to_dict(orient='list') for n, g in df.groupby('name')]
        return Portfolio(name, spec_list)

    @staticmethod
    def from_Excel(name, ffn, sheet_name, **kwargs):
        """
        read in from Excel

        works via a Pandas dataframe; kwargs passed through to pd.read_excel
        drops all blank columns (mostly for auditing purposes)


        :param name:
        :param ffn: full file name, including path
        :param sheet_name:
        :param kwargs:
        :return:
        """
        df = pd.read_excel(ffn, sheet_name=sheet_name, **kwargs)
        df = df.dropna(axis=1, how='all')
        return Portfolio.from_DataFrame(name, df)
