# -*- coding: utf-8 -*-
#
# Copyright © 2009-2010 CEA
# Pierre Raybaut
# Licensed under the terms of the CECILL License
# Modified for Datadog

import logging
import os
import os.path as osp
import platform
# To manage the agent on OSX
from subprocess import (
    CalledProcessError,
    check_call,
    check_output,
)
import sys
import thread  # To manage the windows process asynchronously

# GUI Imports
from guidata.configtools import (
    add_image_path,
    get_family,
    get_icon,
    MONOSPACE,
)
from guidata.qt.QtCore import (
    QEvent,
    QPoint,
    QSize,
    Qt,
    QTimer,
    SIGNAL,
)
from guidata.qt.QtGui import (
    QApplication,
    QFont,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from guidata.qthelpers import get_std_icon
from spyderlib.widgets.sourcecode.codeeditor import CodeEditor

# small hack to avoid having to patch the spyderlib library
# Needed because of py2exe bundling not being able to access
# the spyderlib image sources
import spyderlib.baseconfig
spyderlib.baseconfig.IMG_PATH = [""]

# Datadog
from checks.check_status import (
    CollectorStatus,
    DogstatsdStatus,
    ForwarderStatus,
    logger_info,
)
from config import (
    get_confd_path,
    get_config,
    get_config_path,
    get_logging_config,
    get_version
)
from util import get_os, yLoader

# 3rd Party
import yaml
import tornado.template as template

# Constants describing the agent state
AGENT_RUNNING = 0
AGENT_START_PENDING = 1
AGENT_STOP_PENDING = 2
AGENT_STOPPED = 3
AGENT_UNKNOWN = 4

# Import Windows stuff only on Windows
if get_os() == 'windows':
    import win32serviceutil
    import win32service
    WIN_STATUS_TO_AGENT = {
        win32service.SERVICE_RUNNING: AGENT_RUNNING,
        win32service.SERVICE_START_PENDING: AGENT_START_PENDING,
        win32service.SERVICE_STOP_PENDING: AGENT_STOP_PENDING,
        win32service.SERVICE_STOPPED: AGENT_STOPPED,
    }

log = logging.getLogger(__name__)

EXCLUDED_WINDOWS_CHECKS = [
    'btrfs', 'cacti', 'directory', 'docker', 'gearmand',
    'hdfs', 'kafka_consumer', 'marathon', 'mcache',
    'mesos', 'network', 'postfix', 'process',
    'gunicorn', 'zk', 'ssh_check'
]

EXCLUDED_MAC_CHECKS = [
    'win32_event_log', 'windows_service', 'wmi_check',
    'iis', 'sqlserver', 'cacti'
]

MAIN_WINDOW_TITLE = "Datadog Agent Manager"

DATADOG_SERVICE = "DatadogAgent"

HUMAN_SERVICE_STATUS = {
    AGENT_RUNNING: 'Agent is running',
    AGENT_START_PENDING: 'Agent is starting',
    AGENT_STOP_PENDING: 'Agent is stopping',
    AGENT_STOPPED: 'Agent is stopped',
    AGENT_UNKNOWN: "Cannot get Agent status",
}

REFRESH_PERIOD = 5000

OPEN_LOG = "Open log file"


def get_checks():
    checks = {}
    conf_d_directory = get_confd_path(get_os())

    for filename in sorted(os.listdir(conf_d_directory)):
        module_name, ext = osp.splitext(filename)
        if get_os() == 'windows':
            excluded_checks = EXCLUDED_WINDOWS_CHECKS
        else:
            excluded_checks = EXCLUDED_MAC_CHECKS
        if filename.split('.')[0] in excluded_checks:
            continue
        if ext not in ('.yaml', '.example', '.disabled'):
            continue

        agent_check = AgentCheck(filename, ext, conf_d_directory)
        if (agent_check.enabled or agent_check.module_name not in checks or
           (not agent_check.is_example and not checks[agent_check.module_name].enabled)):
            checks[agent_check.module_name] = agent_check

    checks_list = checks.values()
    checks_list.sort(key=lambda c: c.module_name)

    return checks_list


class EditorFile(object):
    def __init__(self, file_path, description):
        self.file_path = file_path
        self.description = description

    def get_description(self):
        return self.description

    def save(self, content):
        try:
            f = open(self.file_path, 'w')
            f.write(content)
            self.content = content
            info_popup("File saved.")
        except Exception, e:
            warning_popup("Unable to save file: \n %s" % str(e))
            raise


class DatadogConf(EditorFile):
    def __init__(self, config_path):
        EditorFile.__init__(self, config_path, "Agent settings file: datadog.conf")

    @property
    def api_key(self):
        config = get_config(parse_args=False, cfg_path=self.file_path)
        api_key = config.get('api_key', None)
        if not api_key or api_key == 'APIKEYHERE':
            return None
        return api_key

    def check_api_key(self, editor):
        if self.api_key is None:
            api_key, ok = QInputDialog.getText(
                None, "Add your API KEY",
                "You must first set your api key in this file."
                " You can find it here: https://app.datadoghq.com/account/settings#api"
            )
            if ok and api_key:
                new_content = []
                for line in self.content.splitlines():
                    if "api_key:" in line:
                        new_content.append("api_key: %s" % str(api_key))
                    else:
                        new_content.append("%s" % line)
                new_content = "\n".join(new_content)
                self.save(new_content)
                editor.set_text(new_content)

                if agent_status() != AGENT_STOPPED:
                    agent_manager("restart")
                else:
                    agent_manager("start")
            elif not ok:
                warning_popup("The agent needs an API key to send metrics to Datadog")
                if agent_status() != AGENT_STOPPED:
                    agent_manager("stop")
            else:
                self.check_api_key(editor)


class AgentCheck(EditorFile):
    def __init__(self, filename, ext, conf_d_directory):
        file_path = osp.join(conf_d_directory, filename)
        self.module_name = filename.split('.')[0]

        EditorFile.__init__(self, file_path, description=self.module_name.replace("_", " ").title())

        self.enabled = ext == '.yaml'
        self.is_example = ext == '.example'
        self.enabled_name = osp.join(conf_d_directory, "%s.yaml" % self.module_name)
        self.disabled_name = "%s.disabled" % self.enabled_name

    def enable(self):
        self.enabled = True
        os.rename(self.file_path, self.enabled_name)
        self.file_path = self.enabled_name

    def disable(self):
        self.enabled = False
        os.rename(self.file_path, self.disabled_name)
        self.file_path = self.disabled_name

    def save(self, content):
        check_yaml_syntax(content)
        EditorFile.save(self, content)


class PropertiesWidget(QWidget):
    def __init__(self, parent):
        QWidget.__init__(self, parent)
        font = QFont(get_family(MONOSPACE), 10, QFont.Normal)

        info_icon = QLabel()
        icon = get_std_icon('MessageBoxInformation').pixmap(24, 24)
        info_icon.setPixmap(icon)
        info_icon.setFixedWidth(32)
        info_icon.setAlignment(Qt.AlignTop)

        self.service_status_label = QLabel()
        self.service_status_label.setWordWrap(True)
        self.service_status_label.setAlignment(Qt.AlignTop)
        self.service_status_label.setFont(font)

        self.desc_label = QLabel()
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignTop)
        self.desc_label.setFont(font)

        self.group_desc = QGroupBox("Description", self)
        layout = QHBoxLayout()
        layout.addWidget(info_icon)
        layout.addWidget(self.desc_label)
        layout.addStretch()
        layout.addWidget(self.service_status_label)

        self.group_desc.setLayout(layout)

        self.editor = CodeEditor(self)
        self.editor.setup_editor(linenumbers=True, font=font)
        self.editor.setReadOnly(False)
        self.group_code = QGroupBox("Source code", self)
        layout = QVBoxLayout()
        layout.addWidget(self.editor)
        self.group_code.setLayout(layout)

        self.enable_button = QPushButton(get_icon("apply.png"),
                                         "Enable", self)

        self.save_button = QPushButton(get_icon("filesave.png"),
                                       "Save", self)

        self.disable_button = QPushButton(get_icon("delete.png"),
                                          "Disable", self)

        self.refresh_button = QPushButton(get_icon("restart.png"),
                                          "Refresh", self)
        hlayout = QHBoxLayout()
        hlayout.addWidget(self.save_button)
        hlayout.addWidget(self.enable_button)
        hlayout.addWidget(self.disable_button)
        hlayout.addWidget(self.refresh_button)

        vlayout = QVBoxLayout()
        vlayout.addWidget(self.group_desc)
        vlayout.addWidget(self.group_code)
        self.html_window = HTMLWindow()
        vlayout.addWidget(self.html_window)

        vlayout.addLayout(hlayout)
        self.setLayout(vlayout)

        self.current_file = None

    def set_status(self):
        self.refresh_button.setEnabled(True)
        self.disable_button.setEnabled(False)
        self.enable_button.setEnabled(False)
        self.save_button.setEnabled(False)

    def set_item(self, check):
        self.refresh_button.setEnabled(False)
        self.save_button.setEnabled(True)
        self.current_file = check
        self.desc_label.setText(check.get_description())
        self.editor.set_text_from_file(check.file_path)
        check.content = self.editor.toPlainText().__str__()
        if check.enabled:
            self.disable_button.setEnabled(True)
            self.enable_button.setEnabled(False)
        else:
            self.disable_button.setEnabled(False)
            self.enable_button.setEnabled(True)

    def set_datadog_conf(self, datadog_conf):
        self.save_button.setEnabled(True)
        self.refresh_button.setEnabled(False)
        self.current_file = datadog_conf
        self.desc_label.setText(datadog_conf.get_description())
        self.editor.set_text_from_file(datadog_conf.file_path)
        datadog_conf.content = self.editor.toPlainText().__str__()
        self.disable_button.setEnabled(False)
        self.enable_button.setEnabled(False)
        datadog_conf.check_api_key(self.editor)

    def set_log_file(self, log_file):
        self.save_button.setEnabled(False)
        self.refresh_button.setEnabled(True)
        self.disable_button.setEnabled(False)
        self.enable_button.setEnabled(False)
        try:
            self.current_file = log_file
            self.desc_label.setText(log_file.get_description())
            self.editor.set_text_from_file(log_file.file_path)
            log_file.content = self.editor.toPlainText().__str__()
            self.editor.go_to_line(len(log_file.content.splitlines()))
        except Exception:
            self.editor.set_text("Log file not found")


class HTMLWindow(QTextEdit):
    def __init__(self, parent=None):
        QTextEdit.__init__(self, parent)
        self.setReadOnly(True)
        self.setHtml(self.latest_status())

    def latest_status(self):
        try:
            loaded_template = template.Loader(".")
            dogstatsd_status = DogstatsdStatus.load_latest_status()
            forwarder_status = ForwarderStatus.load_latest_status()
            collector_status = CollectorStatus.load_latest_status()
            generated_template = loaded_template.load("status.html").generate(
                port=22,
                platform=platform.platform(),
                agent_version=get_version(),
                python_version=platform.python_version(),
                logger_info=logger_info(),
                dogstatsd=dogstatsd_status.to_dict(),
                forwarder=forwarder_status.to_dict(),
                collector=collector_status.to_dict(),
                )
            return generated_template
        except Exception:
            return ("Unable to fetch latest status")


class MainWindow(QSplitter):
    def __init__(self, parent=None):
        current_os = get_os()
        prefix_conf = ''

        if current_os == 'windows':
            prefix_conf = 'windows_'
        else:
            add_image_path(osp.join(os.getcwd(), 'images'))
            # add datadog-agent in PATH
            os.environ['PATH'] = "{0}:{1}".format(
                osp.join(os.getcwd(), '../MacOS'),
                os.environ['PATH']
            )

        log_conf = get_logging_config()

        QSplitter.__init__(self, parent)
        self.setWindowTitle(MAIN_WINDOW_TITLE)
        self.setWindowIcon(get_icon("agent.svg"))

        self.sysTray = SystemTray(self)

        self.connect(self.sysTray, SIGNAL("activated(QSystemTrayIcon::ActivationReason)"), self.__icon_activated)

        checks = get_checks()
        datadog_conf = DatadogConf(get_config_path())
        self.create_logs_files_windows(log_conf, prefix_conf)

        listwidget = QListWidget(self)
        listwidget.addItems([osp.basename(check.module_name).replace("_", " ").title() for check in checks])

        self.properties = PropertiesWidget(self)

        self.setting_button = QPushButton(get_icon("info.png"),
                                          "Logs and Status", self)
        self.menu_button = QPushButton(get_icon("settings.png"),
                                       "Actions", self)
        self.settings = [
            ("Forwarder Logs", lambda: [self.properties.set_log_file(self.forwarder_log_file),
                self.show_html(self.properties.group_code, self.properties.html_window, False)]),
            ("Collector Logs", lambda: [self.properties.set_log_file(self.collector_log_file),
                self.show_html(self.properties.group_code, self.properties.html_window, False)]),
            ("Dogstatsd Logs", lambda: [self.properties.set_log_file(self.dogstatsd_log_file),
                self.show_html(self.properties.group_code, self.properties.html_window, False)]),
            ("JMX Fetch Logs", lambda: [self.properties.set_log_file(self.jmxfetch_log_file),
                self.show_html(self.properties.group_code, self.properties.html_window, False)]),
            ("Agent Status", lambda: [self.properties.html_window.setHtml(self.properties.html_window.latest_status()),
                self.show_html(self.properties.group_code, self.properties.html_window, True),
                self.properties.set_status()]),
        ]

        self.agent_settings = QPushButton(get_icon("edit.png"),
                                          "Settings", self)
        self.connect(self.agent_settings, SIGNAL("clicked()"),
                     lambda: [self.properties.set_datadog_conf(datadog_conf),
                     self.show_html(self.properties.group_code, self.properties.html_window, False)])

        self.setting_menu = SettingMenu(self.settings)
        self.connect(self.setting_button, SIGNAL("clicked()"),
                     lambda: self.setting_menu.popup(self.setting_button.mapToGlobal(QPoint(0, 0))))

        self.manager_menu = Menu(self)
        self.connect(self.menu_button, SIGNAL("clicked()"),
                     lambda: self.manager_menu.popup(self.menu_button.mapToGlobal(QPoint(0, 0))))

        holdingBox = QGroupBox("", self)
        Box = QVBoxLayout(self)
        Box.addWidget(self.agent_settings)
        Box.addWidget(self.setting_button)
        Box.addWidget(self.menu_button)
        Box.addWidget(listwidget)
        holdingBox.setLayout(Box)

        self.addWidget(holdingBox)
        self.addWidget(self.properties)

        self.connect(self.properties.enable_button, SIGNAL("clicked()"),
                     lambda: enable_check(self.properties))

        self.connect(self.properties.disable_button, SIGNAL("clicked()"),
                     lambda: disable_check(self.properties))

        self.connect(self.properties.save_button, SIGNAL("clicked()"),
                     lambda: save_file(self.properties))

        self.connect(self.properties.refresh_button, SIGNAL("clicked()"),
                     lambda: [self.properties.set_log_file(self.properties.current_file),
                     self.properties.html_window.setHtml(self.properties.html_window.latest_status())])

        self.connect(listwidget, SIGNAL('currentRowChanged(int)'),
                     lambda row: [self.properties.set_item(checks[row]),
                     self.show_html(self.properties.group_code, self.properties.html_window, False)])

        listwidget.setCurrentRow(0)

        self.setSizes([150, 1])
        self.setStretchFactor(1, 1)
        self.resize(QSize(950, 600))
        self.properties.set_datadog_conf(datadog_conf)

        self.do_refresh()

    def do_refresh(self):
        try:
            if self.isVisible():
                service_status = agent_status()
                self.properties.service_status_label.setText(HUMAN_SERVICE_STATUS[service_status])
        finally:
            QTimer.singleShot(REFRESH_PERIOD, self.do_refresh)

    def closeEvent(self, event):
        self.hide()
        self.sysTray.show()
        event.ignore()

    def __icon_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show()

    def show_html(self, editor, html, state):
        if state is True:
            editor.setVisible(False)
            html.setVisible(True)
        else:
            editor.setVisible(True)
            html.setVisible(False)

    def create_logs_files_windows(self, config, prefix):
        self.forwarder_log_file = EditorFile(
            config.get('%sforwarder_log_file' % prefix),
            "Forwarder log file"
        )
        self.collector_log_file = EditorFile(
            config.get('%scollector_log_file' % prefix),
            "Collector log file"
        )
        self.dogstatsd_log_file = EditorFile(
            config.get('%sdogstatsd_log_file' % prefix),
            "Dogstatsd log file"
        )
        self.jmxfetch_log_file = EditorFile(
            config.get('jmxfetch_log_file'),
            "JMX log file"
        )

    def show(self):
        QSplitter.show(self)
        self.raise_()


class Menu(QMenu):
    START_AGENT = "Start Agent"
    STOP_AGENT = "Stop Agent"
    RESTART_AGENT = "Restart Agent"
    EXIT_MANAGER = "Exit Agent Manager"

    def __init__(self, parent=None):
        QMenu.__init__(self, parent)
        self.options = {}
        system_tray_menu = [
            (self.START_AGENT, lambda: agent_manager("start")),
            (self.STOP_AGENT, lambda: agent_manager("stop")),
            (self.RESTART_AGENT, lambda: agent_manager("restart")),
            (self.EXIT_MANAGER, lambda: sys.exit(0)),
        ]

        for name, action in system_tray_menu:
            menu_action = self.addAction(name)
            self.connect(menu_action, SIGNAL('triggered()'), action)
            self.options[name] = menu_action

        self.connect(self, SIGNAL("aboutToShow()"), lambda: self.update_options())

    def update_options(self):
        status = agent_status()
        if status == AGENT_RUNNING:
            self.options[self.START_AGENT].setEnabled(False)
            self.options[self.RESTART_AGENT].setEnabled(True)
            self.options[self.STOP_AGENT].setEnabled(True)
        elif status == AGENT_STOPPED:
            self.options[self.START_AGENT].setEnabled(True)
            self.options[self.RESTART_AGENT].setEnabled(False)
            self.options[self.STOP_AGENT].setEnabled(False)
        elif status in [AGENT_START_PENDING, AGENT_STOP_PENDING]:
            self.options[self.START_AGENT].setEnabled(False)
            self.options[self.RESTART_AGENT].setEnabled(False)
            self.options[self.STOP_AGENT].setEnabled(False)


class SettingMenu(QMenu):

    def __init__(self, settings, parent=None,):
        QMenu.__init__(self, parent)
        self.options = {}

        for name, action in settings:
            menu_action = self.addAction(name)
            self.connect(menu_action, SIGNAL('triggered()'), action)
            self.options[name] = menu_action


class SystemTray(QSystemTrayIcon):

    def __init__(self, parent=None):
        QSystemTrayIcon.__init__(self, parent)
        self.setIcon(get_icon("agent.png"))
        self.setVisible(True)
        self.options = {}

        menu = Menu(self.parent())
        self.setContextMenu(menu)


def disable_check(properties):
    check = properties.current_file
    new_content = properties.editor.toPlainText().__str__()

    if check.content != new_content:
        warning_popup("You must first save the file.")
        return

    properties.enable_button.setEnabled(True)
    properties.disable_button.setEnabled(False)
    check.disable()


def enable_check(properties):
    check = properties.current_file

    new_content = properties.editor.toPlainText().__str__()
    if check.content != new_content:
        warning_popup("You must first save the file")
        return

    properties.enable_button.setEnabled(False)
    properties.disable_button.setEnabled(True)
    check.enable()


def save_file(properties):
    current_file = properties.current_file
    new_content = properties.editor.toPlainText().__str__()
    current_file.save(new_content)


def check_yaml_syntax(content):
    try:
        yaml.load(content, Loader=yLoader)
    except Exception, e:
        warning_popup("Unable to parse yaml: \n %s" % str(e))
        raise


def service_manager(action):
    try:
        if action == 'stop':
            win32serviceutil.StopService(DATADOG_SERVICE)
        elif action == 'start':
            win32serviceutil.StartService(DATADOG_SERVICE)
        elif action == 'restart':
            win32serviceutil.RestartService(DATADOG_SERVICE)
    except Exception, e:
        warning_popup("Couldn't %s service: \n %s" % (action, str(e)))


def service_manager_status():
    try:
        return WIN_STATUS_TO_AGENT[
            win32serviceutil.QueryServiceStatus(DATADOG_SERVICE)[1]
        ]
    except Exception:
        return AGENT_UNKNOWN


def osx_manager(action):
    try:
        check_call(['datadog-agent', action])
    except Exception, e:
        warning_popup("Couldn't execute datadog-agent %s: \n %s" % (action, str(e)))


def osx_manager_status():
    try:
        check_output(['datadog-agent', 'status'])
        return AGENT_RUNNING
    except CalledProcessError, e:
        if 'not running' in e.output:
            return AGENT_STOPPED
        elif 'STARTING' in e.output:
            return AGENT_START_PENDING
        elif 'STOPPED' in e.output:
            return AGENT_STOP_PENDING
        else:
            return AGENT_UNKNOWN


def agent_status():
    if get_os() == 'windows':
        return service_manager_status()
    else:
        return osx_manager_status()


def agent_manager(action, async=True):
    if get_os() == 'windows':
        manager = service_manager
    else:
        manager = osx_manager
    if not async:
        manager(action)
    else:
        thread.start_new_thread(manager, (action,))


def warning_popup(message, parent=None):
    QMessageBox.warning(parent, 'Message', message, QMessageBox.Ok)


def info_popup(message, parent=None):
    QMessageBox.information(parent, 'Message', message, QMessageBox.Ok)


class AgentManagerApp(QApplication):
    def __init__(self):
        QApplication.__init__(self, [])
        QApplication.setQuitOnLastWindowClosed(False)
        self.main_window = None

    def event(self, e):
        if e.type() == QEvent.Type.ApplicationActivate:
            self.main_window.show()
        print 'Inside event(): %s' % e.type()
        return QApplication.event(self, e)

    def set_main_window(self, main_window):
        self.main_window = main_window


if __name__ == '__main__':
    app = AgentManagerApp()
    win = MainWindow()
    app.set_main_window(win)
    win.show()
    app.exec_()
