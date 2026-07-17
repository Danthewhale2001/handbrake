/*
 * hb_preview — generate preview images using HandBrake's internal libhb
 * Usage: hb_preview <input> <outdir> <num_previews>
 * Outputs: <outdir>/preview_000.jpg, preview_001.jpg, ...
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <turbojpeg.h>
#include "handbrake/handbrake.h"

static int write_jpeg(const char *path, hb_image_t *img)
{
    int w = img->width;
    int h = img->height;

    if (!img->plane[0].data) return -1;

    int stride = img->plane[0].stride;
    unsigned char *src = img->plane[0].data;
    unsigned char *rgb = (unsigned char*)malloc(w * h * 3);
    if (!rgb) return -1;

    if (stride == w * 4) {
        /* Packed BGRA */
        for (int row = 0; row < h; row++) {
            for (int col = 0; col < w; col++) {
                unsigned char *px = src + row * stride + col * 4;
                rgb[(row * w + col) * 3 + 0] = px[2]; /* R */
                rgb[(row * w + col) * 3 + 1] = px[1]; /* G */
                rgb[(row * w + col) * 3 + 2] = px[0]; /* B */
            }
        }
    } else if (stride == w * 3) {
        /* Packed BGR */
        for (int row = 0; row < h; row++) {
            for (int col = 0; col < w; col++) {
                unsigned char *px = src + row * stride + col * 3;
                rgb[(row * w + col) * 3 + 0] = px[2]; /* R */
                rgb[(row * w + col) * 3 + 1] = px[1]; /* G */
                rgb[(row * w + col) * 3 + 2] = px[0]; /* B */
            }
        }
    } else {
        free(rgb);
        return -1;
    }

    tjhandle tj = tjInitCompress();
    if (!tj) { free(rgb); return -1; }

    unsigned char *jpegbuf = NULL;
    unsigned long jpegsize = 0;
    int ret = tjCompress2(tj, rgb, w, w * 3, h, TJPF_RGB,
                          &jpegbuf, &jpegsize, TJSAMP_420, 85, 0);
    free(rgb);

    if (ret != 0) { tjDestroy(tj); return -1; }

    FILE *f = fopen(path, "wb");
    if (f) { fwrite(jpegbuf, 1, jpegsize, f); fclose(f); }
    tjFree(jpegbuf);
    tjDestroy(tj);
    return f ? 0 : -1;
}

int main(int argc, char *argv[])
{
    if (argc < 4) {
        fprintf(stderr, "Usage: hb_preview <input> <outdir> <num_previews>\n");
        return 1;
    }
    const char *input  = argv[1];
    const char *outdir = argv[2];
    int nprev = atoi(argv[3]);
    if (nprev < 1)  nprev = 1;
    if (nprev > 30) nprev = 30;

    mkdir(outdir, 0755);

    if (hb_global_init() < 0) return 1;

    hb_handle_t *hb = hb_init(0);
    if (!hb) return 1;

    hb_list_t *paths = hb_list_init();
    hb_list_add(paths, (void*)input);
    hb_scan(hb, paths, 1, nprev, 0, 0, 0, 0, 0, NULL, 0, 0);
    hb_list_close(&paths);

    hb_state_t state;
    int ticks = 0;
    while (1) {
        hb_get_state(hb, &state);
        if (state.state == HB_STATE_SCANDONE) break;
        if (++ticks > 600) { hb_close(&hb); return 1; }
        hb_snooze(100);
    }

    hb_list_t *titles = hb_get_titles(hb);
    if (!titles || hb_list_count(titles) == 0) { hb_close(&hb); return 1; }

    hb_title_t *title = (hb_title_t*)hb_list_item(titles, 0);
    hb_job_t *job = hb_job_init(title);
    if (!job) { hb_close(&hb); return 1; }

    hb_dict_t *job_dict = hb_job_to_dict(job);
    hb_job_close(&job);
    if (!job_dict) { hb_close(&hb); return 1; }

    int written = 0;
    for (int i = 0; i < nprev; i++) {
        hb_image_t *img = hb_get_preview3(hb, i, job_dict);
        if (!img) continue;
        char path[4096];
        snprintf(path, sizeof(path), "%s/preview_%03d.jpg", outdir, i);
        if (write_jpeg(path, img) == 0) {
            printf("%s\n", path);
            fflush(stdout);
            written++;
        }
        hb_image_close(&img);
    }

    hb_value_free(&job_dict);
    hb_close(&hb);
    hb_global_close();

    return written > 0 ? 0 : 1;
}
