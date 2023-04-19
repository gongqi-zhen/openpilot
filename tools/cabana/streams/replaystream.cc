#include "tools/cabana/streams/replaystream.h"

#include <QLabel>
#include <QFileDialog>
#include <QGridLayout>
#include <QMessageBox>
#include <QPushButton>

#include "common/prefix.h"

ReplayStream::ReplayStream(QObject *parent) : AbstractStream(parent, false) {
  QObject::connect(&settings, &Settings::changed, [this]() {
    if (replay) replay->setSegmentCacheLimit(settings.max_cached_minutes);
  });
}

ReplayStream::~ReplayStream() {
  if (replay) replay->stop();
}

static bool event_filter(const Event *e, void *opaque) {
  return ((ReplayStream *)opaque)->eventFilter(e);
}

void ReplayStream::mergeSegments() {
  for (auto &[n, seg] : replay->segments()) {
    if (seg && seg->isLoaded() && !processed_segments.count(n)) {
      const auto &events = seg->log->events;
      bool append = processed_segments.empty() || *processed_segments.rbegin() < n;
      processed_segments.insert(n);
      mergeEvents(events.cbegin(), events.cend(), append);
    }
  }
}

bool ReplayStream::loadRoute(const QString &route, const QString &data_dir, uint32_t replay_flags) {
  replay.reset(new Replay(route, {"can", "roadEncodeIdx", "wideRoadEncodeIdx", "carParams"}, {}, nullptr, replay_flags, data_dir, this));
  replay->setSegmentCacheLimit(settings.max_cached_minutes);
  replay->installEventFilter(event_filter, this);
  QObject::connect(replay.get(), &Replay::seekedTo, this, &AbstractStream::seekedTo);
  QObject::connect(replay.get(), &Replay::streamStarted, this, &AbstractStream::streamStarted);
  QObject::connect(replay.get(), &Replay::segmentsMerged, this, &ReplayStream::mergeSegments);
  if (replay->load()) {
    replay->start();
    return true;
  }
  return false;
}

bool ReplayStream::eventFilter(const Event *event) {
  if (event->which == cereal::Event::Which::CAN) {
    updateEvent(event);
  }
  return true;
}

void ReplayStream::pause(bool pause) {
  replay->pause(pause);
  emit(pause ? paused() : resume());
}


AbstractOpenStreamWidget *ReplayStream::widget(AbstractStream **stream) {
  return new OpenReplayWidget(stream);
}

// OpenReplayWidget

static std::unique_ptr<OpenpilotPrefix> op_prefix;

OpenReplayWidget::OpenReplayWidget(AbstractStream **stream) : AbstractOpenStreamWidget(stream) {
  // TODO: get route list from api.comma.ai
  QGridLayout *grid_layout = new QGridLayout();
  grid_layout->addWidget(new QLabel(tr("Route")), 0, 0);
  grid_layout->addWidget(route_edit = new QLineEdit(this), 0, 1);
  route_edit->setPlaceholderText(tr("Enter remote route name or click browse to select a local route"));
  auto file_btn = new QPushButton(tr("Browse..."), this);
  grid_layout->addWidget(file_btn, 0, 2);

  grid_layout->addWidget(new QLabel(tr("Video")), 1, 0);
  grid_layout->addWidget(choose_video_cb = new QComboBox(this), 1, 1);
  QString items[] = {tr("No Video"), tr("Road Camera"), tr("Wide Road Camera"), tr("Driver Camera"), tr("QCamera")};
  for (int i = 0; i < std::size(items); ++i) {
    choose_video_cb->addItem(items[i]);
  }
  choose_video_cb->setCurrentIndex(1);  // default is road camera;

  QVBoxLayout *main_layout = new QVBoxLayout(this);
  main_layout->addLayout(grid_layout);
  setMinimumWidth(550);

  QObject::connect(file_btn, &QPushButton::clicked, [=]() {
    QString dir = QFileDialog::getExistingDirectory(this, tr("Open Local Route"), settings.last_route_dir);
    if (!dir.isEmpty()) {
      route_edit->setText(dir);
      settings.last_route_dir = QFileInfo(dir).absolutePath();
    }
  });
}

bool OpenReplayWidget::open() {
  QString route = route_edit->text();
  QString data_dir;
  if (int idx = route.lastIndexOf('/'); idx != -1) {
    data_dir = route.mid(0, idx + 1);
    route = route.mid(idx + 1);
  }

  bool ret = false;
  bool is_valid_format = Route::parseRoute(route).str.size() > 0;
  if (!is_valid_format) {
    QMessageBox::warning(nullptr, tr("Warning"), tr("Invalid route format: '%1'").arg(route));
  } else {
    // TODO: Remove when OpenpilotPrefix supports ZMQ
#ifndef __APPLE__
    op_prefix.reset(new OpenpilotPrefix());
#endif
    uint32_t flags[] = {REPLAY_FLAG_NO_VIPC, REPLAY_FLAG_NONE, REPLAY_FLAG_ECAM, REPLAY_FLAG_DCAM, REPLAY_FLAG_QCAMERA};
    ReplayStream *replay_stream = *stream ? (ReplayStream *)*stream : new ReplayStream(qApp);
    ret = replay_stream->loadRoute(route, data_dir, flags[choose_video_cb->currentIndex()]);
    if (!ret) {
      if (replay_stream != *stream) {
        delete replay_stream;
      }
      QMessageBox::warning(nullptr, tr("Warning"), tr("Failed to load route: '%1'").arg(route));
    } else {
      *stream = replay_stream;
    }
  }
  return ret;
}
