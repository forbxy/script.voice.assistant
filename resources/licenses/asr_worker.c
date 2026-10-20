/* Persistent sherpa-onnx ASR bridge. No audio devices or networking. */
#include "c-api.h"
#include <stdint.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
  /* family, model A/B/C, tokens, threads, decoding method, hotwords */
  if (argc != 9) return 2;
  pid_t parent = getppid();
  prctl(PR_SET_PDEATHSIG, SIGTERM);
  if (getppid() != parent) return 3;
  struct rlimit core = {0, 0}, memory = {2048UL*1024*1024, 2048UL*1024*1024};
  setrlimit(RLIMIT_CORE, &core);
  setrlimit(RLIMIT_AS, &memory);
  int threads = atoi(argv[6]);
  if (threads < 1 || threads > 4) return 2;
  int streaming = !strcmp(argv[1], "zipformer");
  int max_active_paths = 4;
  float hotwords_score = 2;
  if (streaming) {
    const char *value = getenv("VOICE_ASR_MAX_ACTIVE_PATHS");
    if (value) {
      char *end;
      long parsed = strtol(value, &end, 10);
      if (end == value || *end || parsed < 1 || parsed > 16) return 2;
      max_active_paths = (int)parsed;
    }
    value = getenv("VOICE_ASR_HOTWORDS_SCORE");
    if (value) {
      char *end;
      float parsed = strtof(value, &end);
      if (end == value || *end || !isfinite(parsed) || parsed < 0 || parsed > 10) return 2;
      hotwords_score = parsed;
    }
  }
  const SherpaOnnxOnlineRecognizer *online = NULL;
  const SherpaOnnxOfflineRecognizer *offline = NULL;
  double began = now();
  if (streaming) {
    SherpaOnnxOnlineRecognizerConfig c;
    memset(&c, 0, sizeof(c));
    c.feat_config.sample_rate = 16000;
    c.feat_config.feature_dim = 80;
    c.model_config.num_threads = threads;
    c.model_config.provider = "cpu";
    c.model_config.modeling_unit = "cjkchar";
    c.model_config.tokens = argv[5];
    c.model_config.transducer.encoder = argv[2];
    c.model_config.transducer.decoder = argv[3];
    c.model_config.transducer.joiner = argv[4];
    c.decoding_method = argv[7];
    c.max_active_paths = max_active_paths;
    if (*argv[8]) { c.hotwords_file = argv[8]; c.hotwords_score = hotwords_score; }
    online = SherpaOnnxCreateOnlineRecognizer(&c);
    if (!online) return 4;
  } else {
    SherpaOnnxOfflineRecognizerConfig c;
    memset(&c, 0, sizeof(c));
    c.feat_config.sample_rate = 16000;
    c.feat_config.feature_dim = 80;
    c.model_config.num_threads = threads;
    c.model_config.provider = "cpu";
    c.model_config.tokens = argv[5];
    c.decoding_method = "greedy_search";
    if (!strcmp(argv[1], "fire_red_ctc")) {
      c.model_config.fire_red_asr_ctc.model = argv[2];
    } else if (!strcmp(argv[1], "sensevoice")) {
      c.model_config.sense_voice.model = argv[2];
      c.model_config.sense_voice.use_itn = 1;
    } else {
      return 2;
    }
    offline = SherpaOnnxCreateOfflineRecognizer(&c);
    if (!offline) return 4;
  }
  printf("{\"ready\":true,\"streaming\":%s,\"load_seconds\":%.6f,\"version\":\"%s\"",
         streaming ? "true" : "false", now() - began, SherpaOnnxGetVersionStr());
  if (streaming) printf(",\"max_active_paths\":%d,\"hotwords_score\":%.6g", max_active_paths, hotwords_score);
  puts("}");
  fflush(stdout);
  const SherpaOnnxOnlineStream *on_stream = NULL;
  const SherpaOnnxOfflineStream *off_stream = NULL;
  float *utterance = NULL;
  size_t count = 0, capacity = 0;
  for (;;) {
    uint32_t length;
    if (fread(&length, 4, 1, stdin) != 1) break;
    if (length == UINT32_MAX) {
      if (on_stream) SherpaOnnxDestroyOnlineStream(on_stream);
      if (off_stream) SherpaOnnxDestroyOfflineStream(off_stream);
      on_stream = NULL;
      off_stream = NULL;
      count = 0;
      double stream_began = now();
      if (streaming) on_stream = SherpaOnnxCreateOnlineStream(online);
      else off_stream = SherpaOnnxCreateOfflineStream(offline);
      if (!on_stream && !off_stream) return 5;
      printf("{\"reset\":true,\"create_stream_seconds\":%.6f}\n", now() - stream_began);
      fflush(stdout);
      continue;
    }
    if (length > 32000 || length % 2 || (!on_stream && !off_stream)) return 6;
    int16_t pcm[16000];
    float samples[16000];
    int n = length / 2;
    if (length) {
      if (fread(pcm, 1, length, stdin) != length) return 7;
      for (int i = 0; i < n; ++i) samples[i] = pcm[i] / 32768.0f;
    }
    if (streaming) {
      if (!length) { n = 8000; memset(samples, 0, n * sizeof(float)); }
      SherpaOnnxOnlineStreamAcceptWaveform(on_stream, 16000, samples, n);
      if (!length) SherpaOnnxOnlineStreamInputFinished(on_stream);
      while (SherpaOnnxIsOnlineStreamReady(online, on_stream)) SherpaOnnxDecodeOnlineStream(online, on_stream);
      const char *json = SherpaOnnxGetOnlineStreamResultAsJson(online, on_stream);
      if (!json) return 8;
      printf("{\"result\":%s,\"endpoint\":0}\n", json);
      SherpaOnnxDestroyOnlineStreamResultJson(json);
    } else if (length) {
      if (count + n > 16000 * 40) return 9;
      if (count + n > capacity) {
        capacity = count + n + 16000;
        float *next = realloc(utterance, capacity * sizeof(float));
        if (!next) return 10;
        utterance = next;
      }
      memcpy(utterance + count, samples, n * sizeof(float));
      count += n;
      puts("{\"result\":{\"text\":\"\"},\"endpoint\":0}");
    } else {
      SherpaOnnxAcceptWaveformOffline(off_stream, 16000, utterance, count);
      SherpaOnnxDecodeOfflineStream(offline, off_stream);
      const SherpaOnnxOfflineRecognizerResult *result = SherpaOnnxGetOfflineStreamResult(off_stream);
      if (!result || !result->json) return 8;
      printf("{\"result\":%s,\"endpoint\":0}\n", result->json);
      SherpaOnnxDestroyOfflineRecognizerResult(result);
    }
    if (!length) {
      if (on_stream) SherpaOnnxDestroyOnlineStream(on_stream);
      if (off_stream) SherpaOnnxDestroyOfflineStream(off_stream);
      on_stream = NULL;
      off_stream = NULL;
      count = 0;
    }
    fflush(stdout);
  }
  if (on_stream) SherpaOnnxDestroyOnlineStream(on_stream);
  if (off_stream) SherpaOnnxDestroyOfflineStream(off_stream);
  if (online) SherpaOnnxDestroyOnlineRecognizer(online);
  if (offline) SherpaOnnxDestroyOfflineRecognizer(offline);
  free(utterance);
  return 0;
}
