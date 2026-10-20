/* Diagnostic wrapper around CoreELEC's existing FFmpeg SBC decoder. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
typedef struct { AVCodecContext *context; AVPacket *packet; AVFrame *frame; } R22Decoder;
void r22_decoder_destroy(R22Decoder *d) {
    if (!d) return;
    avcodec_free_context(&d->context); av_packet_free(&d->packet);
    av_frame_free(&d->frame); free(d);
}
R22Decoder *r22_decoder_create(void) {
    if ((avcodec_version() >> 16) != LIBAVCODEC_VERSION_MAJOR) return NULL;
    const AVCodec *codec = avcodec_find_decoder_by_name("sbc");
    if (!codec) return NULL;
    R22Decoder *d = calloc(1, sizeof(*d));
    if (!d) return NULL;
    d->context = avcodec_alloc_context3(codec);
    d->packet = av_packet_alloc(); d->frame = av_frame_alloc();
    if (!d->context || !d->packet || !d->frame) {
        r22_decoder_destroy(d); return NULL;
    }
    d->context->sample_rate = 16000;
    av_channel_layout_default(&d->context->ch_layout, 1);
    if (avcodec_open2(d->context, codec, NULL) < 0) {
        r22_decoder_destroy(d); return NULL;
    }
    return d;
}
int r22_decoder_decode(R22Decoder *d, const uint8_t *input, int length, uint8_t *output, int capacity) {
    if (!d || !input || !output || length != 57 || capacity < 240) return AVERROR(EINVAL);
    av_packet_unref(d->packet);
    int result = av_new_packet(d->packet, length);
    if (result < 0) return result;
    memcpy(d->packet->data, input, length);
    result = avcodec_send_packet(d->context, d->packet);
    if (result < 0) return result;
    av_frame_unref(d->frame);
    result = avcodec_receive_frame(d->context, d->frame);
    if (result < 0) return result;
    if (d->frame->sample_rate != 16000 || d->frame->nb_samples != 120 || d->frame->ch_layout.nb_channels != 1 ||
        (d->frame->format != AV_SAMPLE_FMT_S16 && d->frame->format != AV_SAMPLE_FMT_S16P)) return AVERROR_INVALIDDATA;
    memcpy(output, d->frame->extended_data[0], 240);
    return 240;
}
