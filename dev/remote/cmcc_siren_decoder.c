/* Small CoreELEC wrapper around its existing FFmpeg Siren decoder.
 * The 40-byte ICO frame transformation comes from ICODecoder in the analyzed
 * CMCC APK; the original Android binary is neither loaded nor redistributed.
 * Build with the matching CoreELEC FFmpeg headers/libraries. */
#include <stdint.h>
#include <stdlib.h>
#include <math.h>
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
#include <libavutil/version.h>

typedef struct {
    AVCodecContext *context;
    AVPacket *packet;
    AVFrame *frame;
} CmccDecoder;

void cmcc_decoder_destroy(CmccDecoder *decoder) {
    if (!decoder) return;
    avcodec_free_context(&decoder->context);
    av_packet_free(&decoder->packet);
    av_frame_free(&decoder->frame);
    free(decoder);
}

CmccDecoder *cmcc_decoder_create(void) {
    if ((avcodec_version() >> 16) != LIBAVCODEC_VERSION_MAJOR ||
        (avutil_version() >> 16) != LIBAVUTIL_VERSION_MAJOR) return NULL;
    const AVCodec *codec = avcodec_find_decoder_by_name("siren");
    if (!codec) return NULL;
    CmccDecoder *decoder = calloc(1, sizeof(*decoder));
    if (!decoder) return NULL;
    decoder->context = avcodec_alloc_context3(codec);
    decoder->packet = av_packet_alloc();
    decoder->frame = av_frame_alloc();
    if (!decoder->context || !decoder->packet || !decoder->frame) goto fail;
    decoder->context->sample_rate = 16000;
    decoder->context->bit_rate = 16000;
    av_channel_layout_default(&decoder->context->ch_layout, 1);
    if (avcodec_open2(decoder->context, codec, NULL) < 0) goto fail;
    return decoder;
fail:
    cmcc_decoder_destroy(decoder);
    return NULL;
}

/* Returns 640 PCM bytes, or a negative FFmpeg error. */
int cmcc_decoder_decode(CmccDecoder *decoder, const uint8_t *input,
                        int input_length, uint8_t *output, int output_capacity) {
    static const uint8_t indices[12] = {4,16,9,8,3,11,2,6,18,7,13,17};
    if (!decoder || !input || !output || input_length != 40 || output_capacity < 640)
        return AVERROR(EINVAL);
    uint16_t words[20];
    for (int i = 0; i < 20; i++) words[i] = input[2*i] | (input[2*i+1] << 8);
    for (int i = 0; i < 12; i += 3) {
        int a = indices[i], b = indices[i+1], c = indices[i+2];
        uint16_t original_a = words[a], original_b = words[b];
        words[a] = words[c]; words[b] = original_a; words[c] = original_b;
    }
    av_packet_unref(decoder->packet);
    int error = av_new_packet(decoder->packet, 40);
    if (error < 0) return error;
    for (int i = 0; i < 20; i++) {
        uint16_t word = words[i] ^ 0x0416;
        decoder->packet->data[2*i] = word >> 8;
        decoder->packet->data[2*i+1] = word & 255;
    }
    error = avcodec_send_packet(decoder->context, decoder->packet);
    if (error < 0) return error;
    av_frame_unref(decoder->frame);
    error = avcodec_receive_frame(decoder->context, decoder->frame);
    if (error < 0) return error;
    AVFrame *frame = decoder->frame;
    if (frame->nb_samples != 320 || frame->ch_layout.nb_channels != 1 ||
        (frame->format != AV_SAMPLE_FMT_FLT && frame->format != AV_SAMPLE_FMT_FLTP))
        return AVERROR_INVALIDDATA;
    const float *samples = (const float *)frame->extended_data[0];
    for (int i = 0; i < 320; i++) {
        float value = samples[i] * 32768.0f;
        if (!isfinite(value)) return AVERROR_INVALIDDATA;
        int sample = lrintf(fmaxf(-32768.0f, fminf(32767.0f, value)));
        output[2*i] = sample & 255;
        output[2*i+1] = (sample >> 8) & 255;
    }
    return 640;
}
