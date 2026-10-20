/* Persistent Matcha worker. Private pipe protocol, little-endian ARM64. */
#include "c-api.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <unistd.h>

int main(int argc, char **argv) {
  if (argc != 8) return 2;
  pid_t parent = getppid();
  prctl(PR_SET_PDEATHSIG, SIGTERM);
  if (getppid() != parent) return 3;
  struct rlimit core = {0, 0}, memory = {1500UL*1024*1024,1500UL*1024*1024};
  setrlimit(RLIMIT_CORE, &core); setrlimit(RLIMIT_AS, &memory);
  SherpaOnnxOfflineTtsConfig config;
  memset(&config, 0, sizeof(config));
  config.model.matcha.acoustic_model = argv[1];
  config.model.matcha.vocoder = argv[2];
  config.model.matcha.lexicon = argv[3];
  config.model.matcha.tokens = argv[4];
  config.model.matcha.data_dir = argv[7];
  config.rule_fsts = argv[5];
  config.model.num_threads = atoi(argv[6]);
  config.model.provider = "cpu";
  config.model.matcha.noise_scale = 0.667f;
  config.model.matcha.length_scale = 1.0f;
  config.max_num_sentences = 1; config.silence_scale = 1.0f;
  const SherpaOnnxOfflineTts *tts = SherpaOnnxCreateOfflineTts(&config);
  if (!tts) return 4;
  uint32_t rate = SherpaOnnxOfflineTtsSampleRate(tts);
  fwrite(&rate, 4, 1, stdout); fflush(stdout);
  for (;;) {
    uint32_t length, pause;
    if (fread(&length, 4, 1, stdin) != 1) break;
    if (length == 0 || length > 8192 || fread(&pause, 4, 1, stdin) != 1 || pause > 200) break;
    char text[8193];
    if (fread(text, 1, length, stdin) != length) break;
    text[length] = 0;
    SherpaOnnxGenerationConfig gen;
    memset(&gen, 0, sizeof(gen)); gen.speed = 1; gen.silence_scale = pause / 100.0f;
    const SherpaOnnxGeneratedAudio *a = SherpaOnnxOfflineTtsGenerateWithConfig(tts, text, &gen, NULL, NULL);
    if (!a || a->n <= 0 || a->n > (int)rate * 60) return 5;
    uint32_t size = a->n * 2;
    fwrite(&size, 4, 1, stdout);
    int16_t pcm[4096];
    for (int offset=0; offset<a->n;) {
      int count = a->n-offset; if(count>4096) count=4096;
      for (int i=0;i<count;i++) {
        float v=a->samples[offset+i];
        if (!isfinite(v)) v=0;
        if(v>1) v=1;
        if(v<-1) v=-1;
        pcm[i]=(int16_t)(v*32767);
      }
      if(fwrite(pcm,2,count,stdout)!=(size_t)count) return 6;
      offset+=count;
    }
    fflush(stdout); SherpaOnnxDestroyOfflineTtsGeneratedAudio(a);
  }
  SherpaOnnxDestroyOfflineTts(tts); return 0;
}
