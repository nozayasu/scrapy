import six
import signal

from twisted.internet import reactor, defer

from scrapy.core.engine import ExecutionEngine
from scrapy.resolver import CachingThreadedResolver
from scrapy.extension import ExtensionManager
from scrapy.signalmanager import SignalManager
from scrapy.utils.ossignal import install_shutdown_handlers, signal_names
from scrapy.utils.misc import load_object
from scrapy import log, signals


class Crawler(object):

    def __init__(self, spidercls, settings):
        self.spidercls = spidercls
        self.settings = settings
        self.signals = SignalManager(self)
        self.stats = load_object(self.settings['STATS_CLASS'])(self)
        lf_cls = load_object(self.settings['LOG_FORMATTER'])
        self.logformatter = lf_cls.from_crawler(self)
        self.extensions = ExtensionManager.from_crawler(self)

        # Attribute kept for backward compatibility (Use CrawlerRunner.spiders)
        spman_cls = load_object(self.settings['SPIDER_MANAGER_CLASS'])
        self.spiders = spman_cls.from_settings(self.settings)

        self.crawling = False
        self.spider = None
        self.engine = None

    def install(self):
        # TODO: remove together with scrapy.project.crawler usage
        import scrapy.project
        assert not hasattr(scrapy.project, 'crawler'), "crawler already installed"
        scrapy.project.crawler = self

    def uninstall(self):
        # TODO: remove together with scrapy.project.crawler usage
        import scrapy.project
        assert hasattr(scrapy.project, 'crawler'), "crawler not installed"
        del scrapy.project.crawler

    @defer.inlineCallbacks
    def crawl(self, *args, **kwargs):
        assert not self.crawling, "Crawling already taking place"
        self.crawling = True

        try:
            self.spider = self._create_spider(*args, **kwargs)
            self.engine = self._create_engine()
            start_requests = iter(self.spider.start_requests())
            yield self.engine.open_spider(self.spider, start_requests)
            yield defer.maybeDeferred(self.engine.start)
        except Exception:
            self.crawling = False
            raise

    def _create_spider(self, *args, **kwargs):
        return self.spidercls.from_crawler(self, *args, **kwargs)

    def _create_engine(self):
        return ExecutionEngine(self, lambda _: self.stop())

    @defer.inlineCallbacks
    def stop(self):
        if self.crawling:
            self.crawling = False
            yield defer.maybeDeferred(self.engine.stop)


class CrawlerRunner(object):

    def __init__(self, settings):
        self.settings = settings
        smcls = load_object(settings['SPIDER_MANAGER_CLASS'])
        self.spiders = smcls.from_settings(settings.frozencopy())
        self.crawlers = set()
        self.crawl_deferreds = set()

    def crawl(self, spidercls, *args, **kwargs):
        crawler = self._create_logged_crawler(spidercls)
        self.crawlers.add(crawler)

        crawler.install()
        crawler.signals.connect(crawler.uninstall, signals.engine_stopped)

        d = crawler.crawl(*args, **kwargs)
        self.crawl_deferreds.add(d)
        return d

    def _create_logged_crawler(self, spidercls):
        crawler = self._create_crawler(spidercls)
        log_observer = log.start_from_crawler(crawler)
        if log_observer:
            crawler.signals.connect(log_observer.stop, signals.engine_stopped)
        return crawler

    def _create_crawler(self, spidercls):
        if isinstance(spidercls, six.string_types):
            spidercls = self.spiders.load(spidercls)
        crawler = Crawler(spidercls, self.settings.frozencopy())
        return crawler

    def stop(self):
        return defer.DeferredList(c.stop() for c in self.crawlers)


class CrawlerProcess(CrawlerRunner):
    """A class to run multiple scrapy crawlers in a process simultaneously"""

    def __init__(self, settings):
        super(CrawlerProcess, self).__init__(settings)
        install_shutdown_handlers(self._signal_shutdown)
        self.stopping = False

    def _signal_shutdown(self, signum, _):
        install_shutdown_handlers(self._signal_kill)
        signame = signal_names[signum]
        log.msg(format="Received %(signame)s, shutting down gracefully. Send again to force ",
                level=log.INFO, signame=signame)
        reactor.callFromThread(self.stop)

    def _signal_kill(self, signum, _):
        install_shutdown_handlers(signal.SIG_IGN)
        signame = signal_names[signum]
        log.msg(format='Received %(signame)s twice, forcing unclean shutdown',
                level=log.INFO, signame=signame)
        reactor.callFromThread(self._stop_reactor)

    def start(self, stop_after_crawl=True):
        self._start_logging()
        self._start_reactor(stop_after_crawl)

    def _start_logging(self):
        log.scrapy_info(self.settings)

    def _start_reactor(self, stop_after_crawl=True):
        if stop_after_crawl:
            d = defer.DeferredList(self.crawl_deferreds)
            if d.called:
                # Don't start the reactor if the deferreds are already fired
                return
            d.addBoth(lambda _: self._stop_reactor())
        if self.settings.getbool('DNSCACHE_ENABLED'):
            reactor.installResolver(CachingThreadedResolver(reactor))
        reactor.addSystemEventTrigger('before', 'shutdown', self.stop)
        reactor.run(installSignalHandlers=False)  # blocking call

    def _stop_reactor(self, _=None):
        try:
            reactor.stop()
        except RuntimeError:  # raised if already stopped or in shutdown stage
            pass
